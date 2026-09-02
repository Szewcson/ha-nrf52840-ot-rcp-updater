"""Best-effort safety checks available through OTBR's public REST API."""

from __future__ import annotations

from dataclasses import dataclass
import json
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PreflightError(RuntimeError):
    """The Thread network is busy or the required safety signal is unavailable."""


_BUSY_ACTION_TYPES = {
    "addThreadDeviceTask",
    "getNetworkDiagnosticTask",
    "resetNetworkDiagCounterTask",
    "getEnergyScanTask",
    "updateDeviceCollectionTask",
}
_BUSY_STATES = {"pending", "active", "undiscovered", "attempted"}


@dataclass(frozen=True)
class PreflightResult:
    """The management work observed before a firmware update."""

    quiet_window: int
    actions_checked: int


class OtbrRestClient:
    """Read OTBR's documented REST task queue without touching the RCP serial port."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def actions(self) -> list[dict[str, object]]:
        request = Request(
            f"{self._base_url}/api/actions",
            headers={"Accept": "application/json", "User-Agent": "ha-nrf52840-ot-rcp-updater/0.1"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = response.read(256 * 1024)
        except (HTTPError, URLError, OSError) as err:
            raise PreflightError(f"unable to read OTBR REST actions: {err}") from err
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise PreflightError("OTBR REST actions response is invalid JSON") from err
        if isinstance(document, dict):
            document = document.get("data", document.get("actions"))
        if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
            raise PreflightError("OTBR REST actions response has an unsupported shape")
        return document

    @staticmethod
    def _busy_actions(actions: list[dict[str, object]]) -> list[str]:
        busy: list[str] = []
        for action in actions:
            action_type = action.get("type")
            attributes = action.get("attributes")
            status = attributes.get("status") if isinstance(attributes, dict) else action.get("status")
            if action_type in _BUSY_ACTION_TYPES and status in _BUSY_STATES:
                busy.append(f"{action_type}:{status}")
        return busy

    def require_quiet_management_window(self, quiet_window: int) -> PreflightResult:
        """Reject commissioning or active OTBR management tasks twice over one window.

        OTBR's public REST API does not expose IP counters, so this proves that
        no management task is active. It intentionally does not claim that all
        Thread application traffic is absent.
        """

        first = self.actions()
        busy = self._busy_actions(first)
        if busy:
            raise PreflightError(f"OTBR has active management work: {', '.join(busy)}")
        sleep(quiet_window)
        second = self.actions()
        busy = self._busy_actions(second)
        if busy:
            raise PreflightError(f"OTBR started management work: {', '.join(busy)}")
        return PreflightResult(quiet_window=quiet_window, actions_checked=len(first) + len(second))

    def require_healthy(self) -> None:
        request = Request(f"{self._base_url}/api/node", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise PreflightError(f"OTBR REST node endpoint returned HTTP {response.status}")
                response.read(256 * 1024)
        except (HTTPError, URLError, OSError) as err:
            raise PreflightError(f"OTBR did not become REST-ready: {err}") from err

