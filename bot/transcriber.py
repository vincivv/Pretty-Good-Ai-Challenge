"""
Whisper STT wrapper.

Accepts raw 16-bit PCM bytes (mono, any sample rate) and returns a
transcription string using OpenAI Whisper (whisper-1).  The bytes are
wrapped in a WAV container in-memory so no temp files are created.
"""

import io
import wave

from openai import OpenAI

_client = OpenAI()


def transcribe_audio(pcm_bytes: bytes, sample_rate: int = 8000) -> str:
    """Transcribe raw 16-bit mono PCM audio via OpenAI Whisper.

    Args:
        pcm_bytes:   Raw PCM samples, 16-bit little-endian, mono.
        sample_rate: Sample rate in Hz (default 8 000 — SignalWire mulaw).

    Returns:
        Transcribed text, stripped of leading/trailing whitespace.
        Returns an empty string if the audio is too short to transcribe.
    """
    if len(pcm_bytes) < 3200:  # < 200 ms at 8 kHz – not worth sending
        return ""

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

    buf.seek(0)
    buf.name = "audio.wav"  # SDK uses filename to infer format

    response = _client.audio.transcriptions.create(
        model="whisper-1",
        file=buf,
        language="en",
    )
    return response.text.strip()
