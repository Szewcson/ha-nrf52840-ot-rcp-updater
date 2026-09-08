from __future__ import annotations

import http.client
import io
import json
import struct
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.ingress import IngressApi, IngressError, IngressServer
from app.manual_artifact import DownloadResult
from app.models import (
    Artifact,
    FirmwareRelease,
    PreparedFirmware,
    ncs_dfu_application_version,
)
from app.operation import FlashCommand, OperationBusyError, OperationController, OperationType


def _elf(platform_tag: bytes = b"NRF52840 PCA10059 N/3.4.0 Z/4.4.0") -> bytes:
    header = bytearray(116 + len(platform_tag))
    header[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into(
        "<HHIIIIIHHHHHH",
        header,
        16,
        2,
        40,
        1,
        0x1000,
        52,
        0,
        0x05000000,
        52,
        32,
        2,
        0,
        0,
        0,
    )
    struct.pack_into("<IIIIIIII", header, 52, 1, 0, 0, 0, 52, 52, 4, 4)
    struct.pack_into(
        "<IIIIIIII",
        header,
        84,
        1,
        116,
        0x1000,
        0x1000,
        len(platform_tag),
        len(platform_tag),
        5,
        4,
    )
    header[116:] = platform_tag
    return bytes(header)


def _release() -> FirmwareRelease:
    return FirmwareRelease(
        hardware="PCA10059",
        ncs_version="3.4.0",
        zephyr_version="4.4.0",
        dfu_application_version=ncs_dfu_application_version("3.4.0"),
        artifact=Artifact(
            "https://example.invalid/rcp.elf",
            "0" * 64,
            "rcp.elf",
            "https://example.invalid/rcp.elf.sig",
        ),
        release_url="https://example.invalid/releases/3.4.0",
        release_summary="Test release",
    )


def _prepared(path: Path, source: str, release: FirmwareRelease | None = None) -> PreparedFirmware:
    data = path.read_bytes()
    return PreparedFirmware(
        path=path,
        hardware="PCA10059",
        ncs_version="3.4.0",
        zephyr_version="4.4.0",
        dfu_application_version=ncs_dfu_application_version("3.4.0"),
        sha256=sha256(data).hexdigest(),
        size=len(data),
        source=source,
        release=release,
    )


class _Updater:
    def __init__(self, root: Path, ready: bool = True) -> None:
        self._root = root
        self._ready = ready
        self.preflight_requests: list[PreparedFirmware] = []
        self.release_requests: list[FirmwareRelease] = []

    def prepare_release(self, release: FirmwareRelease) -> PreparedFirmware:
        self.release_requests.append(release)
        path = self._root / "signed-release.elf"
        path.write_bytes(_elf())
        return _prepared(path, "release", release)

    def preflight(self, package: PreparedFirmware) -> dict[str, object]:
        self.preflight_requests.append(package)
        return {
            "ready": self._ready,
            "checks": [
                {
                    "name": "firmware artifact",
                    "state": "ok",
                    "message": "ELF parsed",
                },
                {
                    "name": "RCP serial device",
                    "state": "ok" if self._ready else "error",
                    "message": "configured device present"
                    if self._ready
                    else "configured device absent",
                },
            ],
        }


def _controller(release: FirmwareRelease) -> OperationController:
    controller = OperationController()
    controller.update_runtime(
        installed={
            "hardware": "PCA10059",
            "ncs_version": "3.3.4",
            "zephyr_version": "4.3.99",
        },
        release=release,
        releases=(release,),
        manifest_error=None,
        diagnostics={
            "configured_rcp_device": "/dev/serial/by-id/rcp",
            "configured_rcp_device_present": True,
            "detected_rcp_usb_vid_pid": "1915:0000",
            "detected_rcp_usb_serial": "normal-rcp",
            "rcp_usb_topology_known": True,
            "dfu_target_present": False,
            "otbr_state": "running",
        },
    )
    return controller


class IngressApiTests(unittest.TestCase):
    def test_upload_validation_and_flash_use_one_controller_command_and_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _controller(_release())
            api = IngressApi(controller, _Updater(root), root)
            result = api.validate_upload(io.BytesIO(_elf()), len(_elf()))

            self.assertEqual(result["artifact"]["source"], "upload")
            self.assertTrue(result["artifact_id"])
            flash = api.flash(result["artifact_id"])
            operation = controller.next(0)
            assert operation is not None

            self.assertEqual(flash["state"], "queued")
            self.assertIs(operation.command.operation_type, OperationType.INSTALL_PREPARED)
            self.assertTrue(operation.command.cleanup_path is not None)
            assert operation.command.cleanup_path is not None
            self.assertTrue(operation.command.cleanup_path.exists())
            with self.assertRaisesRegex(IngressError, "expired or was not found"):
                api.flash(result["artifact_id"])
            api.discard_operation_artifact(operation.command)
            self.assertFalse(operation.command.cleanup_path.exists())

    def test_busy_or_failed_preflight_never_leaves_a_flashable_upload(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _controller(_release())
            controller.submit(FlashCommand.install_latest())
            api = IngressApi(controller, _Updater(root), root)
            with self.assertRaises(OperationBusyError):
                api.validate_upload(io.BytesIO(_elf()), len(_elf()))
            self.assertEqual(list(root.iterdir()), [])

            failed_controller = _controller(_release())
            failed_api = IngressApi(failed_controller, _Updater(root, ready=False), root)
            result = failed_api.validate_upload(io.BytesIO(_elf()), len(_elf()))
            self.assertNotIn("artifact_id", result)
            self.assertFalse(result["preflight"]["ready"])
            self.assertEqual(list(root.iterdir()), [])

    def test_release_validation_uses_manifest_release_and_status_is_json_safe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            release = _release()
            controller = _controller(release)
            updater = _Updater(root)
            api = IngressApi(controller, updater, root)

            result = api.validate_release("3.4.0")
            self.assertEqual(updater.release_requests, [release])
            self.assertTrue(result["artifact"]["authenticated"])
            status = api.status()
            self.assertEqual(status["device"]["state"], "connected")
            self.assertEqual(status["firmware"]["policy_target_direction"], "upgrade")
            self.assertEqual(json.loads(json.dumps(status))["firmware"]["latest"]["ncs_version"], "3.4.0")

    def test_status_marks_a_lower_configured_target_as_a_downgrade(self) -> None:
        controller = _controller(_release())
        controller.update_runtime(
            installed={
                "hardware": "PCA10059",
                "ncs_version": "3.5.0-preview1",
                "zephyr_version": "4.5.0",
            },
            release=_release(),
            releases=(_release(),),
            manifest_error=None,
            diagnostics={},
        )
        with TemporaryDirectory() as directory:
            status = IngressApi(controller, _Updater(Path(directory)), Path(directory)).status()

        self.assertEqual(status["firmware"]["policy_target_direction"], "downgrade")

    def test_url_validation_uses_a_server_generated_file_and_expires_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "downloaded.elf"
            path.write_bytes(_elf())
            api = IngressApi(_controller(_release()), _Updater(root), root)
            with patch(
                "app.ingress.download_https_artifact",
                return_value=DownloadResult(path, "https://example.invalid/rcp.elf"),
            ):
                result = api.validate_url("https://example.invalid/rcp.elf")

            artifact_id = result["artifact_id"]
            api._artifacts._entries[artifact_id].expires_at = 0
            api.expire()
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(IngressError, "expired"):
                api.flash(artifact_id)


class IngressServerTests(unittest.TestCase):
    def test_ingress_proxy_gets_api_and_static_ui_but_unmarked_posts_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            api = IngressApi(_controller(_release()), _Updater(root), root)
            server = IngressServer(
                api,
                host="127.0.0.1",
                port=0,
                trusted_proxy_addresses=frozenset({"127.0.0.1"}),
            )
            server.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
                connection.request("GET", "/ingress/api/status", headers={"X-Ingress-Path": "/ingress"})
                response = connection.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["device"]["hardware"], "PCA10059")
                self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))

                connection.request(
                    "POST",
                    "/api/flash",
                    body=b'{"artifact_id":"missing"}',
                    headers={"Content-Type": "application/json", "Content-Length": "25"},
                )
                rejected = connection.getresponse()
                self.assertEqual(rejected.status, 403)
                self.assertEqual(rejected.getheader("Connection"), "close")
                rejected.read()

                connection.request("GET", "/")
                page = connection.getresponse()
                self.assertEqual(page.status, 200)
                self.assertIn(b"Validate and flash firmware", page.read())
                connection.close()
            finally:
                server.stop()

    def test_direct_non_proxy_connection_is_denied(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = IngressServer(
                IngressApi(_controller(_release()), _Updater(root), root),
                host="127.0.0.1",
                port=0,
                trusted_proxy_addresses=frozenset({"192.0.2.1"}),
            )
            server.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
