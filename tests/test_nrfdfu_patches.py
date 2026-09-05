from __future__ import annotations

import unittest
from pathlib import Path


class NrfdfuPatchTests(unittest.TestCase):
    def test_patches_are_focused_and_applied_in_order(self) -> None:
        root = Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"
        patch_directory = root / "patches"
        version_patch = patch_directory / "nrfdfu-cli-init-packet-versions.patch"
        port_patch = patch_directory / "nrfdfu-cli-exact-port.patch"
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        self.assertEqual(
            {path.name for path in patch_directory.glob("*.patch")},
            {version_patch.name, port_patch.name},
        )
        self.assertIn("fw_version: u32", version_patch.read_text(encoding="utf-8"))
        self.assertIn("hw_version: u32", version_patch.read_text(encoding="utf-8"))
        self.assertNotIn("port: Option", version_patch.read_text(encoding="utf-8"))
        self.assertIn("port: Option", port_patch.read_text(encoding="utf-8"))
        self.assertNotIn("fw_version", port_patch.read_text(encoding="utf-8"))
        self.assertNotIn("deduplicate_usb_interfaces", port_patch.read_text(encoding="utf-8"))
        self.assertNotIn("matching_ports.truncate", port_patch.read_text(encoding="utf-8"))

        self.assertLess(
            dockerfile.index(version_patch.name),
            dockerfile.index(port_patch.name),
        )
        self.assertIn("apply --check --unidiff-zero", dockerfile)


if __name__ == "__main__":
    unittest.main()
