# Architecture

## Design Overview

The bot is structured as two cooperating processes: a FastAPI HTTP/WebSocket server that runs in a daemon thread, and a sequential scenario loop that runs on the main thread. When `main.py` starts, it boots the server, opens an ngrok HTTPS tunnel, and then iterates through scenarios one at a time. For each scenario, it calls `set_scenario_context()` to write the active persona into a module-level variable, then uses the SignalWire REST API to dial the target number with the ngrok URL as the webhook. When SignalWire connects the call and the target answers, it POSTs to `/answer`, which returns LaML (SignalWire's TwiML-compatible dialect) that opens a WebSocket back to `/stream`. All subsequent audio flows through that WebSocket: SignalWire sends μ-law encoded audio from the agent in 20 ms chunks, and the bot sends μ-law audio back as the patient. This architecture keeps all logic in a single Python process — no Redis, no queues, no external coordination.

## Call Flow

```
main.py
  │
  ├─ start FastAPI server (daemon thread, port 8000)
  ├─ open ngrok tunnel → public HTTPS URL
  │
  └─ for each scenario:
       ├─ set_scenario_context(scenario, persona)
       ├─ SignalWire REST → outbound call with webhook = ngrok_url/answer
       │
       │   SignalWire ──POST /answer──► FastAPI
       │                                   └─ return LaML <Stream url="/stream"/>
       │
       │   SignalWire ──WebSocket /stream──► FastAPI
       │       μ-law audio (agent → bot)        │
       │       μ-law audio (bot → agent)  ◄─────┘
       │
       ├─ wait for call completion
       └─ analyze_transcript() → append to bug_report.md
```

## Audio Pipeline

```
SignalWire (μ-law 8 kHz) ──► μ-law decode (numpy G.711)
                          ──► accumulate PCM frames
                          ──► RMS silence detection (700 ms / 1200 ms first turn)
                          ──► Whisper STT → patient text
                          ──► GPT-4o-mini (patient persona) → reply text
                          ──► OpenAI TTS (PCM 24 kHz)
                          ──► resample 24 kHz → 8 kHz (pydub anti-aliased)
                          ──► μ-law encode (numpy G.711)
                          ──► stream back over WebSocket to SignalWire
```

## Key Technical Decisions

**SignalWire Media Streams over WebSocket.** SignalWire's LaML `<Stream>` verb opens a bidirectional WebSocket to the ngrok-tunnelled FastAPI server. Audio arrives as base64-encoded μ-law 8 kHz frames every 20 ms. The bot accumulates these frames, detects end-of-turn via RMS silence, then runs the STT → LLM → TTS pipeline and streams the response back the same way.

**Audio codec in pure numpy.** Python's `audioop` module was deprecated in 3.11 and removed in 3.13. Rather than adding a compatibility shim (`audioop-lts`), the ITU G.711 μ-law encode and decode are implemented directly with vectorised numpy operations. The decode formula (byte inversion → sign/exponent/mantissa split → linear magnitude reconstruction) and encode formula (sign extraction → BIAS add → exponent lookup table → mantissa shift → bit inversion) both match the G.711 specification.

**Resampling with pydub.** OpenAI TTS outputs PCM at 24 kHz; SignalWire requires 8 kHz. Pydub's `set_frame_rate()` applies an anti-aliased FIR filter for the downsample — simple decimation (every third sample) would alias and sound distorted.

**Silence-based end-of-turn detection.** Two thresholds are used: 700 ms of consecutive silence (RMS < 300) to end a normal turn, and 1 200 ms for the first turn to give the agent's greeting (often multi-sentence) time to fully finish before the patient responds.

**`ws_open` flag.** When SignalWire closes the WebSocket mid-call (e.g., agent triggers a transfer or triage route), the TTS streaming loop checks `ws_open` before sending each chunk and exits immediately rather than streaming into a dead socket.

**`[[END_CONVERSATION]]` sentinel.** When the patient persona's goal is met, the GPT response appends `[[END_CONVERSATION]]`. The conversation loop strips this token before sending to TTS so it never reaches the audio stream, and then hangs up cleanly. The sentinel is suppressed for the first 5 turns to prevent premature call endings.

**Patient personas.** All 16 personas share the name Margaret Chen (DOB March 14, 1962) — the name registered to the target phone number. Each has a distinct personality, goal, and end condition. The base system prompt enforces: (1) identity-only first response, (2) minimum 5 exchanges before `[[END_CONVERSATION]]`, and (3) transfer refusal (except scenarios 0 and 7 which need it).

**QA analysis.** Post-call, GPT-4o-mini re-reads the transcript with a structured prompt that requires a call summary followed by evidence-backed bug blocks (`Bug:` / `Severity:` / `Timestamp:` / `Details:`). The prompt requires the model to quote the exact agent line where a violation occurred before any bug can be filed. Two hard code-level filters run after the model responds: (1) identity/DOB bugs are stripped when the transcript contains "for demo purposes"; (2) lost-context bugs are stripped unless the patient's re-asked information was actually present in their earlier lines.

## Module Map

| Module | Responsibility |
|---|---|
| `main.py` | CLI, scenario orchestration, FastAPI boot, ngrok tunnel |
| `bot/caller.py` | SignalWire outbound call, recording download |
| `bot/conversation.py` | FastAPI app, WebSocket state machine, STT/LLM/TTS pipeline |
| `bot/personas.py` | 16 patient personas, GPT system prompt builder |
| `bot/analyzer.py` | Post-call QA, bug parsing, report writer |
| `bot/transcriber.py` | Whisper STT wrapper |
| `scenarios/scenarios.json` | 16 scenario definitions with expected behaviour |
