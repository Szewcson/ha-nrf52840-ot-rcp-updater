from __future__ import annotations

import unittest
from pathlib import Path


class ImageLicensingTests(unittest.TestCase):
    def test_runtime_image_carries_project_and_component_evidence(self) -> None:
        root = Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        build_yaml = (root / "build.yaml").read_text(encoding="utf-8")

        self.assertIn("RUST_BUILDER_IMAGE=rust:1.82-alpine@sha256:", dockerfile)
        self.assertRegex(
            dockerfile,
            r"ghcr\.io/home-assistant/amd64-base@sha256:[0-9a-f]{64}",
        )
        self.assertIn("apk add --no-cache", dockerfile)
        for package in (
            "build-base=0.5-r3",
            "ca-certificates=20260413-r0",
            "git=2.45.4-r0",
            "ca-certificates=20260909-r0",
            "python3=3.14.7-r1",
            "py3-cryptography=47.0.0-r0",
            "py3-paho-mqtt=1.6.1-r4",
            "py3-pyserial=3.5-r9",
        ):
            self.assertIn(package, dockerfile)
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
        for architecture in ("aarch64", "amd64"):
            self.assertRegex(
                build_yaml,
                rf"{architecture}: ghcr\.io/home-assistant/{architecture}-base@sha256:[0-9a-f]{{64}}",
            )
        self.assertIn("LICENSES/Apache-2.0.txt", dockerfile)
        self.assertTrue((root / "LICENSES" / "Apache-2.0.txt").is_file())
        self.assertTrue((root / "LICENSES" / "COMPONENTS.md").is_file())


if __name__ == "__main__":
    unittest.main()
