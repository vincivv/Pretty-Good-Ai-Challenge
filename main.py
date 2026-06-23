"""
Medical AI Voice Bot – orchestrator and CLI entry point.

Starts a FastAPI server in a background thread, opens an ngrok tunnel,
then runs each requested scenario: dials the target number, waits for the
call to finish, downloads the recording, and runs post-call QA analysis.
Finally it compiles a summary of bugs found across all scenarios.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from pyngrok import conf as ngrok_conf
from pyngrok import ngrok

load_dotenv()

BASE_DIR = Path(__file__).parent
SCENARIOS_PATH = BASE_DIR / "scenarios" / "scenarios.json"
REPORTS_DIR = BASE_DIR / "reports"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def _start_server(port: int = 8000) -> None:
    """Run uvicorn in a daemon thread (blocks until process exits)."""
    from bot.conversation import app

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def start_server_thread(port: int = 8000) -> None:
    """Spawn the FastAPI server as a background daemon thread."""
    thread = threading.Thread(target=_start_server, args=(port,), daemon=True)
    thread.start()
    time.sleep(2)  # give uvicorn time to bind the port


# ---------------------------------------------------------------------------
# ngrok tunnel
# ---------------------------------------------------------------------------

def open_tunnel(port: int = 8000) -> str:
    """Open an ngrok HTTPS tunnel and return the public base URL."""
    auth_token = os.environ.get("NGROK_AUTHTOKEN", "")
    if not auth_token:
        sys.exit("[ERROR] NGROK_AUTHTOKEN is not set in .env")

    ngrok_conf.get_default().auth_token = auth_token
    tunnel = ngrok.connect(port, "http")
    # pyngrok returns http:// – Twilio requires https://
    url = tunnel.public_url.replace("http://", "https://")
    return url


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

def init_bug_report() -> None:
    """Write the bug report header at the start of a run."""
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / "bug_report.md"
    with open(report_path, "w") as f:
        f.write(f"# Bug Report\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n")
        f.write(f"**Target:** {os.environ.get('TARGET_PHONE_NUMBER', '+18054398008')}  \n\n")
        f.write("---\n")


def run_scenario(
    scenario: dict,
    ngrok_url: str,
    scenario_num: int,
    delay_before: int = 0,
) -> None:
    """Execute one test scenario end-to-end."""
    from bot.analyzer import analyze_transcript
    from bot.caller import SignalWireCaller
    from bot.conversation import set_scenario_context

    if delay_before:
        print(f"\nWaiting {delay_before}s before next call…")
        time.sleep(delay_before)

    print(f"\n{'=' * 60}")
    print(f"  Scenario {scenario_num:02d}: {scenario['name']}")
    print(f"{'=' * 60}")

    # Tell the WebSocket handler which scenario/persona to use
    set_scenario_context(scenario, scenario_num)

    caller = SignalWireCaller(
        project_id=os.environ["SIGNALWIRE_PROJECT_ID"],
        api_token=os.environ["SIGNALWIRE_API_TOKEN"],
        space_url=os.environ["SIGNALWIRE_SPACE_URL"],
        from_number=os.environ["SIGNALWIRE_PHONE_NUMBER"],
        to_number=os.environ.get("TARGET_PHONE_NUMBER", "+18054398008"),
    )

    call_sid = caller.make_call(ngrok_url)
    print(f"  Call SID : {call_sid}")

    final_status = caller.wait_for_completion(call_sid)
    print(f"  Status   : {final_status}")

    # Post-call QA
    transcript_path = TRANSCRIPTS_DIR / f"call_{scenario_num:02d}.txt"
    if not transcript_path.exists():
        # Call never connected — write a stub so the run is always recorded
        TRANSCRIPTS_DIR.mkdir(exist_ok=True)
        stub = "\n".join([
            "=" * 45,
            f"CALL TRANSCRIPT - Call #{scenario_num:02d}",
            f"Scenario: {scenario.get('name', 'Unknown')}",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Duration: 0:00",
            f"Call status: {final_status} — call did not connect, no conversation recorded.",
            "=" * 45,
        ])
        transcript_path.write_text(stub + "\n")
        print(f"    Transcript → {transcript_path}  (stub — call did not connect)")

    # Only run QA if the transcript contains actual conversation turns
    transcript_text = transcript_path.read_text()
    if "[" in transcript_text:
        bugs = analyze_transcript(transcript_path, scenario, scenario_num)
        print(f"  Bugs found: {len(bugs)}")
    else:
        print(f"  [SKIP] No conversation to analyse (call status: {final_status})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Medical AI Voice Bot – automated call tester"
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="Scenario ID (1-18) or 'all'  (default: all)",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=15,
        help="Seconds to wait between calls  (default: 15)",
    )
    args = parser.parse_args()

    # Validate required env vars
    missing = [
        v for v in (
            "OPENAI_API_KEY",
            "SIGNALWIRE_PROJECT_ID",
            "SIGNALWIRE_API_TOKEN",
            "SIGNALWIRE_SPACE_URL",
            "SIGNALWIRE_PHONE_NUMBER",
            "NGROK_AUTHTOKEN",
        )
        if not os.environ.get(v)
    ]
    if missing:
        sys.exit(f"[ERROR] Missing env vars: {', '.join(missing)}")

    with open(SCENARIOS_PATH) as f:
        all_scenarios = json.load(f)

    # Filter to requested scenario(s)
    if args.scenario.lower() == "all":
        scenarios_to_run = all_scenarios
    else:
        idx = int(args.scenario) - 1
        if idx < 0 or idx >= len(all_scenarios):
            sys.exit(f"[ERROR] Scenario {args.scenario} out of range (1–{len(all_scenarios)})")
        scenarios_to_run = [all_scenarios[idx]]

    print("=" * 60)
    print("  Medical AI Voice Bot – starting up")
    print(f"  Scenarios : {len(scenarios_to_run)}")
    print(f"  Delay     : {args.delay}s between calls")
    print("=" * 60)

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    # Boot FastAPI + ngrok
    start_server_thread(port=8000)
    print("  FastAPI server started on :8000")

    ngrok_url = open_tunnel(port=8000)
    print(f"  ngrok URL : {ngrok_url}")

    init_bug_report()

    # Run scenarios sequentially
    try:
        for i, scenario in enumerate(scenarios_to_run):
            delay = args.delay if i > 0 else 0
            run_scenario(
                scenario=scenario,
                ngrok_url=ngrok_url,
                scenario_num=scenario["id"],
                delay_before=delay,
            )
    finally:
        ngrok.disconnect(ngrok_url)
        print(f"\n{'=' * 60}")
        print("  All scenarios complete.")
        print(f"  Bug report : {REPORTS_DIR / 'bug_report.md'}")
        print("=" * 60)


if __name__ == "__main__":
    main()
