from __future__ import annotations

import base64
import json
import struct
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.manifest import FirmwareManifest, ManifestError, _validate_rcp_elf, download_artifact


def _signature(payload: bytes) -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signature = b"ed25519:" + base64.b64encode(private_key.sign(payload)) + b"\n"
    return public_key, signature


def _elf() -> bytes:
    header = bytearray(120)
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
    struct.pack_into("<IIIIIIII", header, 84, 1, 116, 0x1000, 0x1000, 4, 4, 5, 4)
    header[116:] = b"RCP!"
    return bytes(header)


def _release(ncs_version: str) -> dict[str, object]:
    return {
        "hardware": "PCA10059",
        "ncs_version": ncs_version,
        "zephyr_version": "4.4.0",
        "dfu_application_version": 3_004_000,
        "artifact": {
            "url": f"https://example.invalid/{ncs_version}.elf",
            "sha256": "0" * 64,
            "filename": f"{ncs_version}.elf",
            "signature_url": f"https://example.invalid/{ncs_version}.elf.sig",
        },
        "release_url": "https://example.invalid/release",
        "release_summary": "Test release",
    }


class FirmwareManifestTests(unittest.TestCase):
    def test_selects_newest_ncs_not_project_release_label(self) -> None:
        manifest = FirmwareManifest.from_bytes(
            json.dumps(
                {
                    "schema_version": 1,
                    "releases": [_release("3.3.4"), _release("3.4.0")],
                }
            ).encode()
        )
        self.assertEqual(manifest.newest_for("PCA10059").ncs_version, "3.4.0")

    def test_keeps_prereleases_opt_in_and_honors_minor_pin(self) -> None:
        manifest = FirmwareManifest.from_bytes(
            json.dumps(
                {
                    "schema_version": 1,
                    "releases": [
                        _release("3.4.0"),
                        _release("3.5.0-preview1"),
                        _release("3.5.0-rc1"),
                    ],
                }
            ).encode()
        )

        self.assertEqual(manifest.newest_for("PCA10059").ncs_version, "3.4.0")
        self.assertEqual(
            manifest.newest_for("PCA10059", allow_prereleases=True).ncs_version,
            "3.5.0-rc1",
        )
        self.assertEqual(
            manifest.newest_for("PCA10059", allow_prereleases=True, pinned_minor="3.4").ncs_version,
            "3.4.0",
        )

    def test_selects_an_exact_legacy_migration_target(self) -> None:
        manifest = FirmwareManifest.from_bytes(
            json.dumps(
                {
                    "schema_version": 1,
                    "releases": [_release("3.3.4"), _release("3.4.0")],
                }
            ).encode()
        )
        self.assertEqual(manifest.release_for("PCA10059", "3.3.4").ncs_version, "3.3.4")

    def test_lists_manifest_targets_for_the_runtime_selector(self) -> None:
        manifest = FirmwareManifest.from_bytes(
            json.dumps(
                {
                    "schema_version": 1,
                    "releases": [_release("3.3.4"), _release("3.4.0"), _release("3.5.0-rc1")],
                }
            ).encode()
        )

        self.assertEqual(
            [release.ncs_version for release in manifest.releases_for("PCA10059")],
            ["3.3.4", "3.4.0"],
        )
        self.assertEqual(
            [
                release.ncs_version
                for release in manifest.releases_for("PCA10059", allow_prereleases=True)
            ],
            ["3.3.4", "3.4.0", "3.5.0-rc1"],
        )

    def test_accepts_a_bounded_arm_elf(self) -> None:
        _validate_rcp_elf(_elf())

    def test_rejects_a_wrong_artifact_type(self) -> None:
        release = _release("3.4.0")
        release["artifact"] = {
            "url": "https://example.invalid/rcp.zip",
            "sha256": "0" * 64,
            "filename": "rcp.zip",
            "signature_url": "https://example.invalid/rcp.zip.sig",
        }
        with self.assertRaisesRegex(ManifestError, "simple .elf filename"):
            FirmwareManifest.from_bytes(
                json.dumps({"schema_version": 1, "releases": [release]}).encode()
            )

    def test_rejects_a_non_arm_elf(self) -> None:
        invalid = bytearray(_elf())
        struct.pack_into("<H", invalid, 18, 62)
        with self.assertRaisesRegex(ManifestError, "ARM executable"):
            _validate_rcp_elf(bytes(invalid))

    def test_verifies_elf_checksum_before_writing(self) -> None:
        data = _elf()
        public_key, signature = _signature(data)
        entry = _release("3.4.0")
        entry["artifact"] = {
            "url": "https://example.invalid/rcp.elf",
            "sha256": sha256(data).hexdigest(),
            "filename": "rcp.elf",
            "signature_url": "https://example.invalid/rcp.elf.sig",
        }
        release = FirmwareManifest.from_bytes(
            json.dumps({"schema_version": 1, "releases": [entry]}).encode()
        ).newest_for("PCA10059")

        with TemporaryDirectory() as directory:
            with (
                patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", public_key),
                patch("app.manifest._fetch", side_effect=[data, signature]),
            ):
                downloaded = download_artifact(release, Path(directory))

            self.assertEqual(downloaded.read_bytes(), data)

    def test_verifies_the_manifest_before_parsing_it(self) -> None:
        payload = json.dumps(
            {"schema_version": 1, "releases": [_release("3.4.0")]}
        ).encode()
        public_key, signature = _signature(payload)

        with (
            patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", public_key),
            patch("app.manifest._fetch", side_effect=[payload, signature]),
        ):
            manifest = FirmwareManifest.download("https://example.invalid/manifest.json")

        self.assertEqual(manifest.newest_for("PCA10059").ncs_version, "3.4.0")

    def test_rejects_a_manifest_with_a_wrong_detached_signature(self) -> None:
        payload = json.dumps(
            {"schema_version": 1, "releases": [_release("3.4.0")]}
        ).encode()
        public_key, signature = _signature(b"different manifest")

        with (
            patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", public_key),
            patch("app.manifest._fetch", side_effect=[payload, signature]),
            self.assertRaisesRegex(ManifestError, "signature does not match"),
        ):
            FirmwareManifest.download("https://example.invalid/manifest.json")


if __name__ == "__main__":
    unittest.main()
