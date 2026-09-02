"""Supervisor calls with explicit OTBR ownership transitions."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SupervisorError(RuntimeError):
    """Home Assistant Supervisor did not complete an OTBR lifecycle action."""


class SupervisorClient:
    """Minimal Supervisor client using the app's scoped SUPERVISOR_TOKEN."""

    def __init__(self, base_url: str = "http://supervisor") -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise SupervisorError("SUPERVISOR_TOKEN is unavailable; enable hassio_api")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def addon_state(self, addon_slug: str) -> str:
        response = self._request("GET", f"/addons/{addon_slug}/info")
        data = response.get("data", response)
        state = data.get("state") if isinstance(data, dict) else None
        if not isinstance(state, str):
            raise SupervisorError(f"Supervisor did not provide state for {addon_slug}")
        return state.lower()

    def stop_addon(self, addon_slug: str) -> None:
        self._request("POST", f"/addons/{addon_slug}/stop")

    def start_addon(self, addon_slug: str) -> None:
        self._request("POST", f"/addons/{addon_slug}/start")

    def wait_for_state(self, addon_slug: str, expected: set[str], timeout: float = 30) -> None:
        deadline = monotonic() + timeout
        last_state = "unknown"
        while monotonic() < deadline:
            last_state = self.addon_state(addon_slug)
            if last_state in expected:
                return
            sleep(0.5)
        raise SupervisorError(
            f"{addon_slug} did not reach {sorted(expected)}; last state was {last_state}"
        )

    @contextmanager
    def temporarily_stop(self, addon_slug: str):
        """Stop OTBR once and restore it exactly once even when flashing fails."""

        state = self.addon_state(addon_slug)
        was_running = state in {"started", "running"}
        if was_running:
            self.stop_addon(addon_slug)
            self.wait_for_state(addon_slug, {"stopped", "not_running"})
        try:
            yield was_running
        finally:
            if was_running:
                self.start_addon(addon_slug)
                self.wait_for_state(addon_slug, {"started", "running"}, timeout=45)

    def _request(self, method: str, path: str) -> dict[str, object]:
        request = Request(
            f"{self._base_url}{path}",
            method=method,
            headers=self._headers,
            data=b"{}" if method == "POST" else None,
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read(512 * 1024)
        except (HTTPError, URLError, OSError) as err:
            raise SupervisorError(f"Supervisor request {method} {path} failed: {err}") from err
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise SupervisorError(f"Supervisor returned invalid JSON for {path}") from err
        if not isinstance(document, dict) or document.get("result") not in {"ok", None}:
            raise SupervisorError(f"Supervisor rejected {method} {path}: {document}")
        return document

