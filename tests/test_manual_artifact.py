from __future__ import annotations

import io
import stat
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.manual_artifact import (
    MAX_MANUAL_ARTIFACT_BYTES,
    ManualArtifactError,
    _resolve_global_addresses,
    download_https_artifact,
    prepare_manual_firmware,
    write_upload,
)
from app.models import ncs_dfu_application_version


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


class _OverlongReader:
    def read(self, size: int) -> bytes:
        return b"x" * (size + 1)


class ManualArtifactTests(unittest.TestCase):
    def test_upload_is_private_bounded_and_produces_a_tagged_prepared_firmware(self) -> None:
        data = _elf()
        with TemporaryDirectory() as directory:
            path = write_upload(io.BytesIO(data), len(data), Path(directory))
            package = prepare_manual_firmware(path, "upload")

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(package.ncs_version, "3.4.0")
            self.assertEqual(package.zephyr_version, "4.4.0")
            self.assertEqual(
                package.dfu_application_version, ncs_dfu_application_version("3.4.0")
            )
            self.assertEqual(package.size, len(data))
            self.assertEqual(package.source, "upload")

    def test_incomplete_or_overlong_upload_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ManualArtifactError, "ended before"):
                write_upload(io.BytesIO(b"short"), 6, root)
            with self.assertRaisesRegex(ManualArtifactError, "exceeded"):
                write_upload(_OverlongReader(), 1, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_upload_rejects_a_size_above_the_bounded_ingress_limit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ManualArtifactError, "4194304"):
                write_upload(io.BytesIO(), MAX_MANUAL_ARTIFACT_BYTES + 1, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_a_structurally_valid_elf_without_the_exact_platform_tag(self) -> None:
        data = _elf(b"NRF52840 PCA10040 N/3.4.0 Z/4.4.0")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.elf"
            path.write_bytes(data)
            with self.assertRaisesRegex(ManualArtifactError, "platform tag"):
                prepare_manual_firmware(path, "upload")

    def test_url_download_rejects_non_https_before_any_network_activity(self) -> None:
        with self.assertRaisesRegex(ManualArtifactError, "HTTPS"):
            download_https_artifact("http://127.0.0.1/rcp.elf")

    def test_url_dns_rejects_private_addresses_and_deduplicates_public_ones(self) -> None:
        with patch(
            "app.manual_artifact.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ManualArtifactError, "local or private"):
                _resolve_global_addresses("example.invalid", 443)

        with patch(
            "app.manual_artifact.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("1.1.1.1", 443)),
            ],
        ):
            self.assertEqual(
                _resolve_global_addresses("example.invalid", 443), ("8.8.8.8", "1.1.1.1")
            )


if __name__ == "__main__":
    unittest.main()
