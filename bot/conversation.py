"""
Real-time WebSocket audio handler for SignalWire Media Streams.

Conversation state machine per call:

  WAITING ──► AGENT_SPEAKING ──► TRANSCRIBING ──► THINKING ──► PATIENT_SPEAKING
     ▲                                                                  │
     └──────────────────────────────────────────────────────────────────┘
                        (loop until DONE or call hangs up)

Audio pipeline (inbound):
  SignalWire mulaw 8 kHz  →  mulaw_decode()  →  PCM16 buffer
  Silence detected?   →  transcribe_audio()  →  text

Audio pipeline (outbound):
  text  →  GPT-4o-mini  →  patient text
  patient text  →  OpenAI TTS (PCM 24 kHz)  →  resample to 8 kHz
  →  mulaw_encode()  →  SignalWire mulaw 8 kHz
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import wave
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import math

import numpy as np
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from openai import AsyncOpenAI
from scipy.signal import resample_poly

# ─── Module-level scenario state (set by main.py before each call) ────────────

_scenario_context: dict = {}
_scenario_number: int = 0


def set_scenario_context(scenario: dict, number: int) -> None:
    """Store the active scenario so the WebSocket handler can read it."""
    global _scenario_context, _scenario_number
    _scenario_context = scenario
    _scenario_number = number


# ─── Constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 8_000           # SignalWire mulaw sample rate (Hz)
SILENCE_THRESHOLD = 300.0     # RMS below this value → treat chunk as silence
SILENCE_MS = 700              # consecutive silent ms required to end a turn
SILENCE_SAMPLE_COUNT = int(SAMPLE_RATE * SILENCE_MS / 1_000)  # 5 600 samples
FIRST_TURN_SILENCE_MS = 1_200  # longer threshold for the agent's opener (multi-sentence)
FIRST_TURN_SILENCE_COUNT = int(SAMPLE_RATE * FIRST_TURN_SILENCE_MS / 1_000)  # 12 000 samples
CHUNK_SIZE = 160              # outbound mulaw samples per media message (~20 ms)
MAX_TURNS = 20                # hard cap to prevent infinite loops

BASE_DIR = Path(__file__).parent.parent

# ─── OpenAI async client ──────────────────────────────────────────────────────

_ai_client = AsyncOpenAI()

# ─── FastAPI application ──────────────────────────────────────────────────────

app = FastAPI()


# ─── Audio codec — pure numpy, no audioop dependency ─────────────────────────

_EXP_LUT = np.array(
    [
        0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
        4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    ],
    dtype=np.int32,
)

_CLIP = 32_767
_BIAS = 0x84  # 132


def mulaw_decode(data: bytes) -> bytes:
    """ITU G.711 μ-law → 16-bit linear PCM, vectorised via numpy.

    Args:
        data: Raw mulaw bytes from SignalWire (8-bit, 8 kHz).

    Returns:
        16-bit little-endian PCM bytes at the same sample rate.
    """
    u = (~np.frombuffer(data, dtype=np.uint8)).astype(np.int32)
    t = ((u & 0x0F) << 3) + _BIAS
    t = t << ((u & 0x70) >> 4)
    pcm = np.where(u & 0x80, _BIAS - t, t - _BIAS).astype(np.int16)
    return pcm.tobytes()


def mulaw_encode(pcm_bytes: bytes) -> bytes:
    """16-bit linear PCM → ITU G.711 μ-law, vectorised via numpy.

    Args:
        pcm_bytes: 16-bit little-endian mono PCM bytes.

    Returns:
        8-bit mulaw bytes ready to send to SignalWire.
    """
    pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.int32)
    sign = np.where(pcm < 0, np.int32(0), np.int32(0x80))
    pcm = np.abs(pcm)
    pcm = np.minimum(pcm + _BIAS, _CLIP)
    idx = ((pcm >> 7) & 0xFF).astype(np.intp)
    exp = _EXP_LUT[idx]
    mantissa = (pcm >> (exp + 3)) & 0x0F
    result = (~(sign | (exp << 4) | mantissa)) & 0xFF
    return result.astype(np.uint8).tobytes()


def rms(pcm_bytes: bytes) -> float:
    """Return the RMS amplitude of a 16-bit PCM buffer."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0


def resample_pcm(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM audio using scipy (anti-aliased polyphase filter).

    Args:
        pcm_bytes: Raw 16-bit mono PCM.
        from_rate: Source sample rate in Hz.
        to_rate:   Target sample rate in Hz.

    Returns:
        Resampled 16-bit mono PCM bytes.
    """
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    gcd = math.gcd(from_rate, to_rate)
    up, down = to_rate // gcd, from_rate // gcd          # e.g. 1, 3 for 24k→8k
    resampled = resample_poly(samples, up, down)
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


# ─── Call state ───────────────────────────────────────────────────────────────


class CallState(Enum):
    WAITING = auto()           # idle, waiting for agent to speak
    AGENT_SPEAKING = auto()    # receiving non-silent audio from agent
    TRANSCRIBING = auto()      # sending buffered audio to Whisper
    THINKING = auto()          # waiting for GPT response
    PATIENT_SPEAKING = auto()  # streaming TTS audio back to SignalWire
    DONE = auto()              # conversation complete


# ─── HTTP endpoint ────────────────────────────────────────────────────────────


@app.post("/answer")
async def answer_call(request: Request) -> Response:
    """Return TwiML instructing SignalWire to open a Media Stream WebSocket.

    The host header carries the ngrok hostname so we never need to hard-code
    the public URL — it's derived dynamically from each incoming request.
    """
    host = request.headers.get("host", "localhost:8000")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="wss://{host}/stream" />'
        "</Connect>"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/call-status")
async def call_status(_request: Request) -> Response:
    """No-op SignalWire status callback — prevents 404 errors in logs."""
    return Response(status_code=204)


# ─── WebSocket endpoint ───────────────────────────────────────────────────────


@app.websocket("/stream")
async def stream_websocket(websocket: WebSocket) -> None:
    """Handle a SignalWire Media Stream connection for one phone call.

    Receives μ-law audio from the agent, detects end-of-turn via silence,
    transcribes with Whisper, generates a patient reply with GPT-4o-mini,
    converts to speech with OpenAI TTS, and streams the audio back.
    """
    await websocket.accept()

    scenario = _scenario_context.copy()
    scenario_num = _scenario_number

    # ── per-call state ────────────────────────────────────────────────────────
    state = CallState.WAITING
    audio_buffer = bytearray()   # raw PCM16 bytes for the current agent turn
    inbound_pcm = bytearray()    # full inbound recording (all agent audio)
    outbound_pcm = bytearray()   # full outbound recording (patient TTS audio)
    silence_samples = 0          # consecutive silent samples counted
    has_speech = False           # have we received any speech this turn?
    is_processing = False        # prevent overlapping process_agent_turn calls
    first_turn_done = False      # use longer silence gate until opener finishes
    ws_open = True               # set False when remote closes the WebSocket

    conversation_history: list[dict] = []
    transcript_lines: list[str] = []
    start_time = time.time()
    stream_sid: str | None = None
    turn_count = 0

    # ── helper: elapsed timestamp for transcript ──────────────────────────────

    def elapsed() -> str:
        s = int(time.time() - start_time)
        return f"{s // 60:02d}:{s % 60:02d}"

    def log(speaker: str, text: str) -> None:
        line = f"[{elapsed()}] {speaker}: {text}"
        transcript_lines.append(line)
        print(f"    {line}")

    # ── helper: send a mulaw buffer to SignalWire in real-time 20 ms chunks ─────────

    async def _send_mulaw_buffer(mulaw_bytes: bytes) -> None:
        nonlocal ws_open
        if not ws_open:
            return
        for i in range(0, len(mulaw_bytes), CHUNK_SIZE):
            chunk = mulaw_bytes[i : i + CHUNK_SIZE]
            if len(chunk) < CHUNK_SIZE:
                chunk += b"\xff" * (CHUNK_SIZE - len(chunk))
            msg = json.dumps(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                }
            )
            try:
                await websocket.send_text(msg)
            except Exception:
                ws_open = False
                print(f"    [DEBUG] WebSocket closed by remote at t={elapsed()} — stopping TTS")
                return
            await asyncio.sleep(0.018)

    # ── helper: stream TTS to SignalWire as chunks arrive (low-latency) ───────────

    async def stream_tts(text: str) -> None:
        """Request TTS and pipe audio to SignalWire as soon as each 100 ms chunk arrives.

        First audio is audible ~200–400 ms after the TTS request is made,
        rather than waiting for the complete response (~1–2 s for longer lines).
        Also accumulates outbound PCM into outbound_pcm, time-aligned with
        inbound_pcm so both tracks can be saved as a stereo recording.
        """
        nonlocal state
        state = CallState.PATIENT_SPEAKING

        # Align outbound timeline to current inbound position.
        # inbound_pcm grows at real-time rate, so its length reflects wall-clock
        # time since call start.  Padding with zeros fills the STT+GPT gap.
        pad = len(inbound_pcm) - len(outbound_pcm)
        if pad > 0:
            outbound_pcm.extend(b"\x00" * pad)

        TTS_CHUNK = 4_800   # 100 ms of 24 kHz 16-bit PCM before resample+send
        buf = bytearray()

        async with _ai_client.audio.speech.with_streaming_response.create(
            model="tts-1",
            voice="nova",
            input=text,
            response_format="pcm",  # 24 kHz 16-bit mono PCM
        ) as tts_stream:
            async for raw in tts_stream.iter_bytes(chunk_size=4_096):
                if not ws_open:
                    break
                buf.extend(raw)
                while len(buf) >= TTS_CHUNK:
                    pcm_8k = resample_pcm(bytes(buf[:TTS_CHUNK]), 24_000, 8_000)
                    outbound_pcm.extend(pcm_8k)
                    await _send_mulaw_buffer(mulaw_encode(pcm_8k))
                    del buf[:TTS_CHUNK]

            if buf and ws_open:  # flush final partial chunk
                pcm_8k = resample_pcm(bytes(buf), 24_000, 8_000)
                outbound_pcm.extend(pcm_8k)
                await _send_mulaw_buffer(mulaw_encode(pcm_8k))

        state = CallState.WAITING

    # ── helper: full agent-turn pipeline ─────────────────────────────────────

    async def process_agent_turn(pcm_bytes: bytes) -> None:
        """Transcribe → generate patient reply → TTS → send audio."""
        nonlocal state, is_processing, turn_count, conversation_history

        if is_processing:
            return
        is_processing = True

        try:
            turn_count += 1
            if turn_count > MAX_TURNS:
                log("SYSTEM", "max turns reached — ending call")
                state = CallState.DONE
                _save_transcript(scenario, scenario_num, transcript_lines, start_time)
                await websocket.close()
                return

            # 1 ── Transcribe agent speech (Whisper, runs in thread pool)
            state = CallState.TRANSCRIBING
            from bot.transcriber import transcribe_audio

            agent_text = await asyncio.to_thread(transcribe_audio, pcm_bytes)
            if not agent_text:
                state = CallState.WAITING
                return

            log("AGENT", agent_text)
            conversation_history.append({"role": "user", "content": agent_text})

            # 2 ── Generate patient response (GPT-4o-mini)
            state = CallState.THINKING
            from bot.personas import get_system_prompt

            chat_resp = await _ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": get_system_prompt(scenario)},
                    *conversation_history,
                ],
                max_tokens=200,
                temperature=0.7,
            )

            raw_text = chat_resp.choices[0].message.content.strip()
            is_done = "[[END_CONVERSATION]]" in raw_text
            patient_text = raw_text.replace("[[END_CONVERSATION]]", "").strip()

            # Never end before 5 full exchanges — the first response is always
            # just an identity confirmation, so this prevents premature exits
            if is_done and turn_count < 5:
                is_done = False
                print(f"    [DEBUG] [[END_CONVERSATION]] suppressed (turn {turn_count} < 5)")

            log("PATIENT", patient_text)
            conversation_history.append({"role": "assistant", "content": patient_text})

            # 3 ── Text-to-speech: stream chunks to SignalWire as they arrive
            await stream_tts(patient_text)

            if is_done:
                state = CallState.DONE
                _save_transcript(scenario, scenario_num, transcript_lines, start_time)
                await websocket.close()
            else:
                state = CallState.WAITING

        except Exception as exc:
            print(f"    [ERROR process_agent_turn] {exc}")
            state = CallState.WAITING
        finally:
            is_processing = False

    # ── main receive loop ─────────────────────────────────────────────────────

    try:
        async for raw in websocket.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                print(
                    f"    Stream started | "
                    f"scenario {scenario_num:02d}: {scenario.get('name', '?')}"
                )

            elif event == "media":
                mulaw_chunk = base64.b64decode(data["media"]["payload"])
                pcm_chunk = mulaw_decode(mulaw_chunk)
                inbound_pcm.extend(pcm_chunk)  # always capture for local recording

                # Ignore inbound audio for turn-detection while we're busy or done
                if state in (
                    CallState.PATIENT_SPEAKING,
                    CallState.TRANSCRIBING,
                    CallState.THINKING,
                    CallState.DONE,
                ):
                    continue

                chunk_rms = rms(pcm_chunk)

                if chunk_rms > SILENCE_THRESHOLD:
                    audio_buffer.extend(pcm_chunk)
                    silence_samples = 0
                    has_speech = True
                    state = CallState.AGENT_SPEAKING
                elif has_speech:
                    # Still counting silence after speech
                    audio_buffer.extend(pcm_chunk)
                    silence_samples += len(pcm_chunk) // 2  # bytes → samples

                    silence_limit = (
                        FIRST_TURN_SILENCE_COUNT if not first_turn_done
                        else SILENCE_SAMPLE_COUNT
                    )
                    if silence_samples >= silence_limit and not is_processing:
                        first_turn_done = True
                        turn_pcm = bytes(audio_buffer)
                        audio_buffer.clear()
                        silence_samples = 0
                        has_speech = False
                        asyncio.create_task(process_agent_turn(turn_pcm))

            elif event == "stop":
                break  # call ended — fall through to finally

    except Exception as exc:
        print(f"    [WebSocket closed by remote] {type(exc).__name__}: {exc}")
    finally:
        _save_transcript(scenario, scenario_num, transcript_lines, start_time)
        _save_recording(bytes(inbound_pcm), bytes(outbound_pcm), scenario_num)


# ─── Transcript writer ────────────────────────────────────────────────────────


def _save_transcript(
    scenario: dict,
    scenario_num: int,
    lines: list[str],
    start_time: float,
) -> None:
    """Write the completed transcript to transcripts/call_NN.txt."""
    transcripts_dir = BASE_DIR / "transcripts"
    transcripts_dir.mkdir(exist_ok=True)

    elapsed_s = int(time.time() - start_time)
    duration = f"{elapsed_s // 60}:{elapsed_s % 60:02d}"
    path = transcripts_dir / f"call_{scenario_num:02d}.txt"

    header_lines = [
        "=" * 45,
        f"CALL TRANSCRIPT - Call #{scenario_num:02d}",
        f"Scenario: {scenario.get('name', 'Unknown')}",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Duration: {duration}",
        f"Recording: recordings/call_{scenario_num:02d}.wav",
        "=" * 45,
    ]

    with open(path, "w") as f:
        f.write("\n".join(header_lines) + "\n")
        if lines:
            f.write("\n".join(lines) + "\n")

    print(f"    Transcript → {path}")


def _save_recording(inbound: bytes, outbound: bytes, scenario_num: int) -> None:
    """Write a stereo WAV — left channel = agent (inbound), right = patient (outbound).

    Both channels are padded to the same length so the timeline is shared.
    The outbound buffer is pre-aligned to wall-clock time in stream_tts, so
    patient speech appears at the correct position relative to the agent.
    """
    if not inbound:
        return
    recordings_dir = BASE_DIR / "recordings"
    recordings_dir.mkdir(exist_ok=True)
    path = recordings_dir / f"call_{scenario_num:02d}.wav"

    # Pad whichever track is shorter so both have equal sample counts
    n_in = len(inbound) // 2   # 16-bit → samples
    n_out = len(outbound) // 2
    n = max(n_in, n_out)

    in_arr = np.frombuffer(inbound, dtype=np.int16)
    out_arr = np.frombuffer(outbound, dtype=np.int16)
    in_arr = np.pad(in_arr, (0, n - n_in))
    out_arr = np.pad(out_arr, (0, n - n_out))

    # Interleave: [L0, R0, L1, R1, …]  (L = agent, R = patient)
    stereo = np.empty(n * 2, dtype=np.int16)
    stereo[0::2] = in_arr
    stereo[1::2] = out_arr

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)            # 16-bit PCM
        wf.setframerate(SAMPLE_RATE)  # 8 000 Hz
        wf.writeframes(stereo.tobytes())

    print(f"    Recording  → {path}")
