from __future__ import annotations

import unittest
from pathlib import Path


class AppArmorProfileTests(unittest.TestCase):
    def test_launcher_has_no_unrestricted_capabilities_or_network(self) -> None:
        profile = (Path(__file__).parents[1] / "nrf52840_ot_rcp_updater" / "apparmor.txt").read_text(
            encoding="utf-8"
        )
        launcher = profile[: profile.index("profile nrf52840_ot_rcp_updater_python")]

        self.assertNotIn("  capability,", launcher)
        self.assertNotIn("  network,", launcher)
        for rule in (
            "network inet stream,",
            "network inet dgram,",
            "network inet6 stream,",
            "network inet6 dgram,",
        ):
            self.assertIn(rule, launcher)

    def test_nrfdfu_uses_a_local_helper_profile(self) -> None:
        profile = (Path(__file__).parents[1] / "nrf52840_ot_rcp_updater" / "apparmor.txt").read_text(
            encoding="utf-8"
        )
        python_profile = profile.index("profile nrf52840_ot_rcp_updater_python")
        helper_profile = profile.index("profile /usr/local/bin/nrfdfu")

        self.assertIn("/usr/local/bin/nrfdfu cx,", profile)
        self.assertLess(helper_profile, self._closing_brace(profile, python_profile))
        self.assertNotIn("cx -> nrf52840_ot_rcp_updater_nrfdfu", profile)

    def test_nrfdfu_can_access_selected_tty_and_verified_firmware_without_network(self) -> None:
        profile = (Path(__file__).parents[1] / "nrf52840_ot_rcp_updater" / "apparmor.txt").read_text(
            encoding="utf-8"
        )
        python_start = profile.index("profile nrf52840_ot_rcp_updater_python")
        helper_start = profile.index("profile /usr/local/bin/nrfdfu")
        python = profile[python_start:helper_start]
        helper = profile[helper_start : self._closing_brace(profile, helper_start)]

        self.assertIn("/data/ rwk,", python)
        self.assertIn("/dev/serial/by-id/** rwk,", python)
        self.assertIn("/dev/ttyACM* rwk,", python)
        self.assertIn("/dev/ttyUSB* rwk,", python)
        for rule in (
            "/data/ r,",
            "/data/downloads/ r,",
            "/data/downloads/*.elf r,",
            "/dev/ttyACM* rwk,",
            "/dev/ttyUSB* rwk,",
            "/sys/ r,",
            "/sys/class/ r,",
            "/sys/class/tty/ r,",
            "/sys/class/tty/** r,",
            "/sys/devices/ r,",
            "/sys/devices/** r,",
        ):
            self.assertIn(rule, helper)
        self.assertNotIn("network", helper)

    @staticmethod
    def _closing_brace(profile: str, start: int) -> int:
        opening = profile.index("{", start)
        depth = 0
        for index, character in enumerate(profile[opening:], opening):
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return index
        raise AssertionError("AppArmor profile has no closing brace")


if __name__ == "__main__":
    unittest.main()
