from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import sys
import unittest
import zipfile


sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.manifest import FirmwareManifest, ManifestError, _validate_dfu_zip


def _release(ncs_version: str) -> dict[str, object]:
    return {
        "hardware": "PCA10059",
        "ncs_version": ncs_version,
        "zephyr_version": "4.4.0",
        "artifact": {
            "url": f"https://example.invalid/{ncs_version}.zip",
            "sha256": "0" * 64,
            "filename": f"{ncs_version}.zip",
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

    def test_rejects_zip_without_a_nordic_dfu_manifest(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("application.bin", b"firmware")
        with self.assertRaises(ManifestError):
            _validate_dfu_zip(buffer.getvalue())

    def test_accepts_a_bounded_nordic_dfu_zip(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("application.bin", b"firmware")
        _validate_dfu_zip(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
