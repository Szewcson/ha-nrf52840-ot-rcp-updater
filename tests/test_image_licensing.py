from __future__ import annotations

import unittest
from pathlib import Path


class ImageLicensingTests(unittest.TestCase):
    def test_runtime_image_carries_project_and_component_evidence(self) -> None:
        root = Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        build_yaml = (root / "build.yaml").read_text(encoding="utf-8")

        self.assertIn("RUST_BUILDER_IMAGE=rust:1.82-alpine@sha256:", dockerfile)
        self.assertIn("ghcr.io/home-assistant/amd64-base:3.24", dockerfile)
        self.assertIn("apk add --no-cache", dockerfile)
        self.assertIn(
            'apk query --from installed --format json --fields name,version "*"',
            dockerfile,
        )
        self.assertIn(r'"{name}\t{version}\n".format', dockerfile)
        self.assertNotIn(r'"{name}\\t{version}\\n".format', dockerfile)
        self.assertIn("py3-cryptography", dockerfile)
        self.assertIn("dependency-metadata.json", dockerfile)
        self.assertIn("alpine-packages.tsv", dockerfile)
        self.assertNotIn("apt-get", dockerfile)
        self.assertNotIn("gcompat", dockerfile)
        self.assertNotIn("debian", build_yaml)
        self.assertIn("aarch64-base:3.24", build_yaml)
        self.assertIn("amd64-base:3.24", build_yaml)
        self.assertIn("LICENSES/Apache-2.0.txt", dockerfile)
        self.assertTrue((root / "LICENSES" / "Apache-2.0.txt").is_file())
        self.assertTrue((root / "LICENSES" / "COMPONENTS.md").is_file())


if __name__ == "__main__":
    unittest.main()
