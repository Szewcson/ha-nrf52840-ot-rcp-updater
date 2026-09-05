from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.models import Settings, ValidationError, version_key


class SettingsTests(unittest.TestCase):
    def test_accepts_home_assistant_list_baudrate_as_a_string(self) -> None:
        settings = Settings.from_mapping(
            {
                "device": "/dev/serial/by-id/nrf52840",
                "baudrate": "460800",
                "safe_update": True,
                "allow_legacy_rcp": False,
                "manifest_poll_interval": 3600,
                "idle_window": 20,
                "boot_timeout": 45,
            }
        )

        self.assertEqual(settings.baudrate, 460800)
        self.assertIsNone(settings.dfu_serial_number)
        self.assertIsNone(settings.dfu_usb_path)
        self.assertFalse(settings.allow_prereleases)
        self.assertIsNone(settings.pinned_ncs_minor)

    def test_uses_defaults_for_every_non_device_option(self) -> None:
        settings = Settings.from_mapping({"device": "/dev/serial/by-id/nrf52840"})

        self.assertEqual(settings.baudrate, 1_000_000)
        self.assertTrue(settings.safe_update)
        self.assertFalse(settings.allow_legacy_rcp)
        self.assertFalse(settings.allow_prereleases)
        self.assertIsNone(settings.dfu_usb_path)
        self.assertEqual(settings.manifest_poll_interval, 3600)
        self.assertEqual(settings.idle_window, 20)
        self.assertEqual(settings.boot_timeout, 90)

    def test_accepts_the_lowest_otbr_baudrate_choice(self) -> None:
        settings = Settings.from_mapping(
            {"device": "/dev/serial/by-id/nrf52840", "baudrate": "57600"}
        )

        self.assertEqual(settings.baudrate, 57600)

    def test_ignores_retired_hardware_identity_and_endpoint_options(self) -> None:
        settings = Settings.from_mapping(
            {
                "device": "/dev/serial/by-id/nrf52840",
                "hardware": "not-supported",
                "manifest_url": "http://untrusted.invalid/manifest.json",
                "otbr_addon_slug": "custom_otbr",
                "otbr_api_url": "not-a-url",
                "dfu_vid_pid": "not-a-vid-pid",
            }
        )

        self.assertFalse(hasattr(settings, "hardware"))
        self.assertFalse(hasattr(settings, "manifest_url"))
        self.assertFalse(hasattr(settings, "otbr_addon_slug"))
        self.assertFalse(hasattr(settings, "otbr_api_url"))

    def test_accepts_nordic_prerelease_and_normalizes_a_minor_pin(self) -> None:
        settings = Settings.from_mapping(
            {
                "device": "/dev/serial/by-id/nrf52840",
                "baudrate": 460800,
                "safe_update": True,
                "allow_legacy_rcp": False,
                "allow_prereleases": True,
                "pinned_ncs_minor": "03.04",
                "manifest_poll_interval": 3600,
                "idle_window": 20,
                "boot_timeout": 45,
            }
        )

        self.assertTrue(settings.allow_prereleases)
        self.assertEqual(settings.pinned_ncs_minor, "3.4")

    def test_accepts_a_usb_path_only_for_ambiguous_bootloader_recovery(self) -> None:
        settings = Settings.from_mapping(
            {
                "device": "/dev/serial/by-id/nrf52840",
                "dfu_usb_path": "2-3.1",
            }
        )

        self.assertEqual(settings.dfu_usb_path, "2-3.1")

    def test_rejects_a_dfu_usb_filesystem_path(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Linux USB path"):
            Settings.from_mapping(
                {
                    "device": "/dev/serial/by-id/nrf52840",
                    "dfu_usb_path": "/sys/bus/usb/devices/2-3",
                }
            )

    def test_orders_preview_rc_and_final_versions(self) -> None:
        self.assertLess(version_key("3.5.0-preview1"), version_key("3.5.0-rc1"))
        self.assertLess(version_key("3.5.0-rc1"), version_key("3.5.0"))


if __name__ == "__main__":
    unittest.main()
