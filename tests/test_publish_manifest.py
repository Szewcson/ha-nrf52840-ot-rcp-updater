from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.publish_manifest import update_manifest


class PublishManifestTests(unittest.TestCase):
    def test_writes_a_manifest_entry_from_build_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            metadata = root / "metadata.json"
            manifest.write_text('{"schema_version": 1, "releases": []}\n', encoding="utf-8")
            metadata.write_text(
                json.dumps(
                    {
                        "hardware": "PCA10059",
                        "ncs_version": "3.3.4",
                        "zephyr_version": "4.4.0",
                        "dfu_application_version": 3_003_004,
                        "artifact": "nrf52840-ot-rcp-ncs-3.3.4.elf",
                        "sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            update_manifest(
                manifest,
                metadata,
                "https://raw.githubusercontent.com/owner/repository/firmware/firmware",
                "https://github.com/owner/repository/tree/firmware",
            )
            document = json.loads(manifest.read_text(encoding="utf-8"))

        release = document["releases"][0]
        self.assertEqual(release["ncs_version"], "3.3.4")
        self.assertEqual(release["dfu_application_version"], 3_003_004)
        self.assertEqual(release["artifact"]["sha256"], "0" * 64)
        self.assertEqual(
            release["artifact"]["url"],
            "https://raw.githubusercontent.com/owner/repository/firmware/firmware/"
            "nrf52840-ot-rcp-ncs-3.3.4.elf",
        )

    def test_orders_preview_rc_and_final_manifest_entries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            metadata = root / "metadata.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "releases": [
                            {
                                "hardware": "PCA10059",
                                "ncs_version": "3.5.0-rc1",
                                "zephyr_version": "4.4.0",
                                "artifact": {},
                                "release_url": "https://example.invalid/rc",
                                "release_summary": "RC",
                            },
                            {
                                "hardware": "PCA10059",
                                "ncs_version": "3.5.0",
                                "zephyr_version": "4.4.0",
                                "artifact": {},
                                "release_url": "https://example.invalid/final",
                                "release_summary": "Final",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            metadata.write_text(
                json.dumps(
                    {
                        "hardware": "PCA10059",
                        "ncs_version": "3.5.0-preview1",
                        "zephyr_version": "4.4.0",
                        "dfu_application_version": 3_004_900,
                        "artifact": "nrf52840-ot-rcp-ncs-3.5.0-preview1.elf",
                        "sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            update_manifest(
                manifest,
                metadata,
                "https://raw.githubusercontent.com/owner/repository/firmware/firmware",
                "https://github.com/owner/repository/tree/firmware",
            )
            document = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(
            [release["ncs_version"] for release in document["releases"]],
            ["3.5.0-preview1", "3.5.0-rc1", "3.5.0"],
        )


if __name__ == "__main__":
    unittest.main()
