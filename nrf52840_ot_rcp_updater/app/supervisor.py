"""Supervisor calls with explicit OTBR ownership transitions."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
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
        operation_error: BaseException | None = None
        try:
            yield was_running
        except BaseException as err:
            operation_error = err
            raise
        finally:
            if was_running:
                try:
                    self._restore_addon(addon_slug)
                except Exception as err:
                    if operation_error is None:
                        raise
                    operation_error.add_note(
                        f"OTBR could not be restarted after the update failure: {err}"
                    )

    def _restore_addon(self, addon_slug: str) -> None:
        """Tolerate Supervisor state propagation while restoring the original owner."""

        last_error: SupervisorError | None = None
        for _ in range(3):
            try:
                if self.addon_state(addon_slug) in {"started", "running"}:
                    return
                self.start_addon(addon_slug)
                self.wait_for_state(addon_slug, {"started", "running"}, timeout=15)
                return
            except SupervisorError as err:
                last_error = err
                sleep(1)
        assert last_error is not None
        raise SupervisorError(
            f"could not restore {addon_slug} after three attempts: {last_error}"
        ) from last_error

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
        except HTTPError as err:
            detail = self._http_error_detail(err)
            raise SupervisorError(
                f"Supervisor request {method} {path} failed: {err}: {detail}"
            ) from err
        except (URLError, OSError) as err:
            raise SupervisorError(f"Supervisor request {method} {path} failed: {err}") from err
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise SupervisorError(f"Supervisor returned invalid JSON for {path}") from err
        if not isinstance(document, dict) or document.get("result") not in {"ok", None}:
            raise SupervisorError(f"Supervisor rejected {method} {path}: {document}")
        return document

    @staticmethod
    def _http_error_detail(error: HTTPError) -> str:
        try:
            payload = error.read(8 * 1024)
        except OSError:
            return "unable to read Supervisor error response"
        if not payload:
            return "no error detail returned"
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload.decode("utf-8", "replace").strip()[:2048]
        if isinstance(document, dict) and isinstance(document.get("message"), str):
            return document["message"][:2048]
        return str(document)[:2048]
