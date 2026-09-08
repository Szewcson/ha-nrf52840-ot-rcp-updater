"""Home Assistant Ingress API and small static frontend for advanced RCP work."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from .manual_artifact import (
    MAX_MANUAL_ARTIFACT_BYTES,
    ManualArtifactError,
    download_https_artifact,
    prepare_manual_firmware,
    write_upload,
)
from .models import (
    SUPPORTED_HARDWARE,
    FirmwareRelease,
    PreparedFirmware,
    ValidationError,
    validate_version,
    version_key,
)
from .operation import FlashCommand, OperationBusyError, OperationController
from .updater import RcpUpdater

LOGGER = logging.getLogger(__name__)
INGRESS_PORT = 8099
_TRUSTED_INGRESS_PROXY = "172.30.32.2"
_MAX_JSON_BYTES = 8 * 1024
_STAGED_ARTIFACT_TTL_SECONDS = 15 * 60
_MAX_STAGED_ARTIFACTS = 3
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class IngressError(RuntimeError):
    """An Ingress request was invalid without exposing an internal detail."""


@dataclass
class _StagedArtifact:
    package: PreparedFirmware
    preflight: dict[str, object]
    display_url: str | None
    cleanup_path: Path | None
    expires_at: float
    claimed: bool = False


class _ArtifactRegistry:
    """Own short-lived uploaded/downloaded ELFs until one command consumes them."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[str, _StagedArtifact] = {}

    def stage(
        self,
        package: PreparedFirmware,
        preflight: dict[str, object],
        display_url: str | None,
        cleanup_path: Path | None,
    ) -> str:
        with self._lock:
            self._remove_expired_locked()
            if len(self._entries) >= _MAX_STAGED_ARTIFACTS:
                raise IngressError("too many validated firmware artifacts are waiting to be flashed")
            identifier = secrets.token_urlsafe(24)
            self._entries[identifier] = _StagedArtifact(
                package=package,
                preflight=preflight,
                display_url=display_url,
                cleanup_path=cleanup_path,
                expires_at=monotonic() + _STAGED_ARTIFACT_TTL_SECONDS,
            )
            return identifier

    def claim(self, identifier: object) -> FlashCommand:
        if not isinstance(identifier, str) or not 16 <= len(identifier) <= 128:
            raise IngressError("a validated firmware artifact is required")
        with self._lock:
            self._remove_expired_locked()
            entry = self._entries.get(identifier)
            if entry is None:
                raise IngressError("validated firmware artifact expired or was not found")
            if entry.claimed:
                raise IngressError("validated firmware artifact is already being flashed")
            entry.claimed = True
            return FlashCommand.install_prepared(entry.package, entry.cleanup_path)

    def release_claim(self, identifier: str) -> None:
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is not None:
                entry.claimed = False

    def consume(self, identifier: str) -> None:
        with self._lock:
            self._entries.pop(identifier, None)

    def discard_path(self, path: Path | None) -> None:
        if path is not None:
            path.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self.discard_path(entry.cleanup_path)

    def expire(self) -> None:
        """Remove abandoned unclaimed artifacts after their short staging window."""

        with self._lock:
            self._remove_expired_locked()

    def _remove_expired_locked(self) -> None:
        expired = [
            identifier
            for identifier, entry in self._entries.items()
            if not entry.claimed and entry.expires_at <= monotonic()
        ]
        for identifier in expired:
            entry = self._entries.pop(identifier)
            self.discard_path(entry.cleanup_path)


class IngressApi:
    """Small, authenticated-proxy-only API that never performs hardware work itself."""

    def __init__(
        self,
        controller: OperationController,
        updater: RcpUpdater,
        temporary_directory: Path = Path("/tmp"),
    ) -> None:
        self._controller = controller
        self._updater = updater
        self._temporary_directory = temporary_directory
        self._artifacts = _ArtifactRegistry()

    def status(self) -> dict[str, object]:
        self.expire()
        runtime = self._controller.runtime_snapshot()
        diagnostics = runtime.diagnostics
        installed = dict(runtime.installed)
        device_present = bool(diagnostics.get("configured_rcp_device_present"))
        dfu_present = bool(diagnostics.get("dfu_target_present"))
        rcp_state = "bootloader" if dfu_present else "connected" if device_present else "unavailable"
        return {
            "device": {
                "hardware": SUPPORTED_HARDWARE,
                "port": diagnostics.get("configured_rcp_device"),
                "usb_vid_pid": diagnostics.get("detected_rcp_usb_vid_pid"),
                "usb_serial": diagnostics.get("detected_rcp_usb_serial"),
                "state": rcp_state,
                "topology_known": bool(diagnostics.get("rcp_usb_topology_known")),
            },
            "firmware": {
                "installed": {
                    "ncs_version": installed.get("ncs_version"),
                    "zephyr_version": installed.get("zephyr_version"),
                    "hardware": installed.get("hardware"),
                },
                "latest": self._release_payload(runtime.release),
                "policy_target_direction": self._policy_target_direction(
                    installed, runtime.release
                ),
                "manifest_error": runtime.manifest_error,
            },
            "otbr": {
                "state": diagnostics.get("otbr_state", "unknown"),
                "error": diagnostics.get("otbr_error"),
            },
            "operation": self._controller.operation_snapshot(),
        }

    def releases(self) -> dict[str, object]:
        self.expire()
        runtime = self._controller.runtime_snapshot()
        return {
            "releases": [self._release_payload(release) for release in runtime.releases],
            "automatic_release": self._release_payload(runtime.release),
            "manifest_error": runtime.manifest_error,
        }

    def operation(self) -> dict[str, object]:
        self.expire()
        return self._controller.operation_snapshot()

    def validate_release(self, ncs_version: object) -> dict[str, object]:
        self._require_idle()
        try:
            version = validate_version(ncs_version, "release version")
        except Exception as err:
            raise IngressError(str(err)) from err
        release = self._controller.release_for(version)
        if release is None:
            raise IngressError("requested release is not available in the selected update channel")
        try:
            package = self._updater.prepare_release(release)
        except Exception as err:
            raise IngressError(f"release could not be prepared: {err}") from err
        return self._validate_and_stage(package, display_url=None, cleanup_path=None)

    def validate_url(self, url: object) -> dict[str, object]:
        self._require_idle()
        result = download_https_artifact(url, self._temporary_directory)
        try:
            package = prepare_manual_firmware(result.path, "url")
            return self._validate_and_stage(
                package, display_url=result.display_url, cleanup_path=result.path
            )
        except Exception:
            result.path.unlink(missing_ok=True)
            raise

    def validate_upload(self, stream: BinaryIO, content_length: int) -> dict[str, object]:
        self._require_idle()
        path = write_upload(stream, content_length, self._temporary_directory)
        try:
            package = prepare_manual_firmware(path, "upload")
            return self._validate_and_stage(package, display_url=None, cleanup_path=path)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def flash(self, artifact_id: object) -> dict[str, object]:
        command = self._artifacts.claim(artifact_id)
        assert isinstance(artifact_id, str)
        try:
            operation_id = self._controller.submit(command)
        except Exception:
            self._artifacts.release_claim(artifact_id)
            raise
        self._artifacts.consume(artifact_id)
        return {"operation_id": operation_id, "state": "queued"}

    def discard_operation_artifact(self, command: FlashCommand) -> None:
        """Remove a one-shot user artifact after the shared updater finishes."""

        self._artifacts.discard_path(command.cleanup_path)

    def close(self) -> None:
        self._artifacts.close()

    def expire(self) -> None:
        self._artifacts.expire()

    def _validate_and_stage(
        self,
        package: PreparedFirmware,
        *,
        display_url: str | None,
        cleanup_path: Path | None,
    ) -> dict[str, object]:
        try:
            preflight = self._updater.preflight(package)
        except Exception as err:
            raise IngressError(f"preflight could not validate the RCP: {err}") from err
        response = {
            "artifact": self._artifact_payload(package, display_url),
            "preflight": preflight,
        }
        if not bool(preflight.get("ready")):
            self._artifacts.discard_path(cleanup_path)
            return response
        artifact_id = self._artifacts.stage(package, preflight, display_url, cleanup_path)
        response["artifact_id"] = artifact_id
        return response

    def _require_idle(self) -> None:
        if bool(self._controller.operation_snapshot()["busy"]):
            raise OperationBusyError("an RCP operation is already pending or active")

    @staticmethod
    def _release_payload(release: FirmwareRelease | None) -> dict[str, object] | None:
        if release is None:
            return None
        return {
            "ncs_version": release.ncs_version,
            "zephyr_version": release.zephyr_version,
            "release_url": release.release_url,
            "summary": release.release_summary,
        }

    @staticmethod
    def _artifact_payload(
        package: PreparedFirmware, display_url: str | None
    ) -> dict[str, object]:
        return {
            "source": package.source,
            "size": package.size,
            "sha256": package.sha256,
            "hardware": package.hardware,
            "ncs_version": package.ncs_version,
            "zephyr_version": package.zephyr_version,
            "authenticated": package.release is not None,
            "url": display_url,
        }

    @staticmethod
    def _policy_target_direction(
        installed: dict[str, object], release: FirmwareRelease | None
    ) -> str:
        """Describe the configured target without claiming HA can surface a downgrade."""

        installed_version = installed.get("ncs_version")
        if release is None or not isinstance(installed_version, str):
            return "unknown"
        try:
            current = version_key(installed_version)
            target = version_key(release.ncs_version)
        except ValidationError:
            return "unknown"
        if target > current:
            return "upgrade"
        if target < current:
            return "downgrade"
        return "current"


class IngressServer:
    """Serve the Ingress UI only to Home Assistant's trusted proxy address."""

    def __init__(
        self,
        api: IngressApi,
        host: str = "0.0.0.0",
        port: int = INGRESS_PORT,
        trusted_proxy_addresses: frozenset[str] = frozenset({_TRUSTED_INGRESS_PROXY}),
    ) -> None:
        self._api = api
        self._host = host
        self._port = port
        self._trusted_proxy_addresses = trusted_proxy_addresses
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._reaper_stop = Event()
        self._reaper_thread: Thread | None = None

    @property
    def port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._port

    def start(self) -> None:
        if self._server is not None:
            return
        handler = self._handler_type()
        server = ThreadingHTTPServer((self._host, self._port), handler)
        server.daemon_threads = True
        self._server = server
        self._thread = Thread(target=server.serve_forever, name="rcp-ingress", daemon=True)
        self._reaper_stop.clear()
        self._reaper_thread = Thread(
            target=self._reap_expired_artifacts,
            name="rcp-ingress-artifact-reaper",
            daemon=True,
        )
        self._thread.start()
        self._reaper_thread.start()
        LOGGER.info("Started Home Assistant Ingress server on port %s", self.port)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        reaper_thread = self._reaper_thread
        self._server = None
        self._thread = None
        self._reaper_thread = None
        self._reaper_stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if reaper_thread is not None:
            reaper_thread.join(timeout=5)
        self._api.close()

    def _reap_expired_artifacts(self) -> None:
        """Bound temporary-file lifetime even when no browser continues polling."""

        while not self._reaper_stop.wait(30):
            self._api.expire()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        api = self._api
        trusted_proxy_addresses = self._trusted_proxy_addresses
        static_directory = Path(__file__).with_name("web")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                # A trusted proxy can still carry a stalled client connection.
                # Keep each lightweight request bounded so uploads cannot retain
                # a worker thread indefinitely.
                self.connection.settimeout(30)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if not self._trusted_proxy():
                    self._json_error(HTTPStatus.FORBIDDEN, "Ingress requests must come from Home Assistant")
                    return
                path = self._relative_path()
                if path == "/api/status":
                    self._json(HTTPStatus.OK, api.status())
                elif path == "/api/releases":
                    self._json(HTTPStatus.OK, api.releases())
                elif path == "/api/operation":
                    self._json(HTTPStatus.OK, api.operation())
                elif path in _STATIC_FILES:
                    self._static(path)
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                if not self._trusted_proxy():
                    self._json_error(HTTPStatus.FORBIDDEN, "Ingress requests must come from Home Assistant")
                    return
                if self.headers.get("X-Requested-With") != "XMLHttpRequest":
                    self._json_error(HTTPStatus.FORBIDDEN, "missing Ingress request header")
                    return
                try:
                    path = self._relative_path()
                    if path == "/api/validate/release":
                        body = self._json_body()
                        response = api.validate_release(body.get("ncs_version"))
                    elif path == "/api/validate/url":
                        body = self._json_body()
                        response = api.validate_url(body.get("url"))
                    elif path == "/api/validate/upload":
                        if self.headers.get_content_type() != "application/octet-stream":
                            raise IngressError("firmware upload must use application/octet-stream")
                        response = api.validate_upload(
                            self.rfile, self._content_length(MAX_MANUAL_ARTIFACT_BYTES)
                        )
                    elif path == "/api/flash":
                        body = self._json_body()
                        response = api.flash(body.get("artifact_id"))
                    else:
                        self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")
                        return
                except OperationBusyError as err:
                    self._json_error(HTTPStatus.CONFLICT, str(err))
                    return
                except (IngressError, ManualArtifactError) as err:
                    self._json_error(HTTPStatus.BAD_REQUEST, str(err))
                    return
                self._json(HTTPStatus.OK, response)

            def _json_body(self) -> dict[str, object]:
                size = self._content_length(_MAX_JSON_BYTES)
                try:
                    decoded = self.rfile.read(size).decode("utf-8")
                    value = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise IngressError("request body must be a JSON object") from err
                if not isinstance(value, dict):
                    raise IngressError("request body must be a JSON object")
                return value

            def _content_length(self, maximum: int) -> int:
                header = self.headers.get("Content-Length")
                if header is None or not header.isascii() or not header.isdecimal():
                    raise IngressError("request requires a valid Content-Length")
                size = int(header)
                if not 1 <= size <= maximum:
                    raise IngressError(f"request body must be between 1 and {maximum} bytes")
                return size

            def _relative_path(self) -> str:
                path = urlsplit(self.path).path
                ingress_path = self.headers.get("X-Ingress-Path", "").rstrip("/")
                if ingress_path and (path == ingress_path or path.startswith(f"{ingress_path}/")):
                    path = path[len(ingress_path) :] or "/"
                return path

            def _trusted_proxy(self) -> bool:
                return self.client_address[0] in trusted_proxy_addresses

            def _static(self, path: str) -> None:
                filename, content_type = _STATIC_FILES[path]
                try:
                    content = (static_directory / filename).read_bytes()
                except OSError:
                    self._json_error(HTTPStatus.NOT_FOUND, "resource was not found")
                    return
                self._bytes(HTTPStatus.OK, content_type, content)

            def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
                self._bytes(
                    status,
                    "application/json; charset=utf-8",
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
                )

            def _json_error(self, status: HTTPStatus, message: str) -> None:
                # POST rejections can happen before a body is read. Closing the
                # connection prevents those unread bytes from becoming a second,
                # malformed request on an HTTP/1.1 keep-alive connection.
                self.close_connection = True
                self._json(status, {"error": str(message)[:512]})

            def _bytes(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if self.close_connection:
                    self.send_header("Connection", "close")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                    "form-action 'self'; img-src 'self'; script-src 'self'; style-src 'self'",
                )
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args
                LOGGER.debug("Ingress HTTP request completed")

        return Handler
