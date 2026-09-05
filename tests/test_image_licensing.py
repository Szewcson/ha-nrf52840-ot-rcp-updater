from __future__ import annotations

import unittest
from pathlib import Path


class ImageLicensingTests(unittest.TestCase):
    def test_runtime_image_carries_project_and_component_evidence(self) -> None:
        root = Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("RUST_BUILDER_IMAGE=rust:1.82-bookworm@sha256:", dockerfile)
        self.assertIn("python3-cryptography", dockerfile)
        self.assertIn("dependency-metadata.json", dockerfile)
        self.assertIn("debian-packages.tsv", dockerfile)
        self.assertIn("LICENSES/Apache-2.0.txt", dockerfile)
        self.assertTrue((root / "LICENSES" / "Apache-2.0.txt").is_file())
        self.assertTrue((root / "LICENSES" / "COMPONENTS.md").is_file())


if __name__ == "__main__":
    unittest.main()
