from __future__ import annotations

import unittest
from pathlib import Path
from tomllib import loads

_CONFIG = Path(__file__).parents[1] / "nrf52840_ot_rcp_updater" / "config.yaml"
_PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


class ConfigSchemaTests(unittest.TestCase):
    def test_names_only_the_supported_pca10059_target(self) -> None:
        config = _CONFIG.read_text(encoding="utf-8")
        project = loads(_PYPROJECT.read_text(encoding="utf-8"))

        self.assertIn("name: PCA10059 OpenThread RCP Updater", config)
        self.assertIn(f"version: {project['project']['version']}", config)

    def test_uses_supervisor_serial_and_baudrate_selectors(self) -> None:
        schema = _CONFIG.read_text(encoding="utf-8").split("schema:\n", maxsplit=1)[1]

        self.assertIn("device: device(subsystem=tty)", schema)
        self.assertIn("baudrate: list(57600|115200|230400|460800|921600|1000000)", schema)
        self.assertIn("qemu_usb_reenumeration_workaround: bool", schema)
        self.assertIn("pinned_ncs_minor: str?", schema)

    def test_uses_only_uart_access_and_an_explicit_confinement_profile(self) -> None:
        config = _CONFIG.read_text(encoding="utf-8")

        self.assertIn("apparmor: true", config)
        self.assertIn("uart: true", config)
        self.assertIn("tmpfs: true", config)
        self.assertNotIn("usb: true", config)

    def test_does_not_expose_fixed_target_or_otbr_settings(self) -> None:
        schema = _CONFIG.read_text(encoding="utf-8").split("schema:\n", maxsplit=1)[1]

        for option in ("hardware", "manifest_url", "otbr_addon_slug", "otbr_api_url", "dfu_vid_pid"):
            self.assertNotIn(f"{option}:", schema)

    def test_removes_retired_options_in_one_supervisor_update(self) -> None:
        run_script = (_CONFIG.parent / "run.sh").read_text(encoding="utf-8")

        self.assertIn(
            "del(.hardware, .manifest_url, .otbr_addon_slug, .otbr_api_url, .dfu_vid_pid)",
            run_script,
        )
        self.assertIn('POST "/addons/self/options"', run_script)
        self.assertNotIn('bashio::addon.option "${option}"', run_script)


if __name__ == "__main__":
    unittest.main()
