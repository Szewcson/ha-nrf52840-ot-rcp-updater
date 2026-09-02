from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


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
                        "artifact": "nrf52840-ot-rcp-ncs-3.3.4.zip",
                        "sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            update_manifest(manifest, metadata, "owner/repository", "rcp-ncs-3.3.4")
            document = json.loads(manifest.read_text(encoding="utf-8"))

        release = document["releases"][0]
        self.assertEqual(release["ncs_version"], "3.3.4")
        self.assertEqual(release["artifact"]["sha256"], "0" * 64)
        self.assertIn("rcp-ncs-3.3.4", release["artifact"]["url"])


if __name__ == "__main__":
    unittest.main()
