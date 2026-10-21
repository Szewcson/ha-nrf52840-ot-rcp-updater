from __future__ import annotations

import re
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
        self.assertNotRegex(dockerfile, r"(?m)^\s+[a-z][a-z0-9+.-]*[=<>~]")
        for package in (
            "build-base",
            "ca-certificates",
            "git",
            "libgcc",
            "libstdc++",
            "python3",
            "py3-cryptography",
            "py3-paho-mqtt",
            "py3-pyserial",
        ):
            # Exact revisions disappear when Alpine publishes branch updates.
            self.assertRegex(dockerfile, rf"(?m)^\s+{re.escape(package)}\s+\\$")
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
