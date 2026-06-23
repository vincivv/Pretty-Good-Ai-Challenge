"""
SignalWire call management: outbound dial, completion polling, recording download.

Flow per scenario:
  1. make_call()           — dials target, registers ngrok webhook, returns SID
  2. wait_for_completion() — polls SignalWire every 3 s until terminal state
  3. download_recording()  — waits for transcoding then saves .mp3

Recording note: SignalWire typically takes 30–60 s after a call ends to make
the recording available.  download_recording() polls up to `max_wait` seconds
and returns False (non-fatal) if it times out.
"""

import time
from pathlib import Path

import requests
from signalwire.rest import Client

_TERMINAL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}


class SignalWireCaller:
    """Manages one outbound SignalWire call from dial to recording download."""

    def __init__(
        self,
        project_id: str,
        api_token: str,
        space_url: str,
        from_number: str,
        to_number: str,
    ) -> None:
        """Initialise with SignalWire credentials and phone numbers.

        Args:
            project_id:  SignalWire Project ID (found in API Credentials).
            api_token:   SignalWire API Token.
            space_url:   Your SignalWire space hostname, e.g. ``yourspace.signalwire.com``.
            from_number: Your SignalWire phone number in E.164 format.
            to_number:   Target phone number in E.164 format.
        """
        self._client = Client(project_id, api_token, signalwire_space_url=space_url)
        self._project_id = project_id
        self._api_token = api_token
        self._space_url = space_url
        self.from_number = from_number
        self.to_number = to_number

    # ── dial ──────────────────────────────────────────────────────────────────

    def make_call(self, ngrok_url: str) -> str:
        """Initiate an outbound call and return the call SID.

        SignalWire will POST to ``{ngrok_url}/answer`` once the target picks up,
        which returns the TwiML that opens the Media Stream WebSocket.

        Args:
            ngrok_url: Public HTTPS base URL (e.g. ``https://abc.ngrok-free.app``).

        Returns:
            Call SID string.
        """
        time.sleep(3)  # avoid exceeding SignalWire's outbound call rate limit
        call = self._client.calls.create(
            to=self.to_number,
            from_=self.from_number,
            url=f"{ngrok_url}/answer",
            record=True,
            status_callback=f"{ngrok_url}/call-status",
            status_callback_method="POST",
        )
        return call.sid

    # ── completion polling ────────────────────────────────────────────────────

    def wait_for_completion(self, call_sid: str, timeout: int = 300) -> str:
        """Block until the call reaches a terminal status.

        Args:
            call_sid: SID returned by :meth:`make_call`.
            timeout:  Maximum seconds to wait (default 5 min).

        Returns:
            Final status string or ``"timeout"``.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            call = self._client.calls(call_sid).fetch()
            if call.status in _TERMINAL_STATUSES:
                return call.status
            time.sleep(3)

        return "timeout"

    # ── recording download ────────────────────────────────────────────────────

    def download_recording(
        self,
        call_sid: str,
        save_path: str,
        max_wait: int = 90,
    ) -> bool:
        """Wait for SignalWire to process the call recording, then download it.

        Args:
            call_sid:  SID of the completed call.
            save_path: Local filesystem path for the output ``.mp3``.
            max_wait:  Seconds to wait for the recording (default 90).

        Returns:
            ``True`` if saved, ``False`` on timeout or HTTP error.
        """
        deadline = time.time() + max_wait

        while time.time() < deadline:
            recordings = self._client.recordings.list(call_sid=call_sid, limit=1)

            if recordings and recordings[0].status == "completed":
                recording = recordings[0]
                mp3_url = (
                    f"https://{self._space_url}"
                    + recording.uri.replace(".json", ".mp3")
                )

                resp = requests.get(
                    mp3_url,
                    auth=(self._project_id, self._api_token),
                    timeout=30,
                )

                if resp.status_code == 200:
                    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(save_path, "wb") as f:
                        f.write(resp.content)
                    print(f"    Recording → {save_path}")
                    return True

                print(f"    [WARN] Recording download HTTP {resp.status_code}")
                return False

            time.sleep(5)

        print(f"    [WARN] Recording not available after {max_wait}s — skipping")
        return False
