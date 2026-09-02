from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.build_rcp import dfu_application_version, platform_info


class BuildRcpTests(unittest.TestCase):
    def test_platform_info_has_only_additive_hardware_and_sdk_tags(self) -> None:
        self.assertEqual(
            platform_info("3.3.4", "4.4.0"),
            "HW/PCA10059 NCS/3.3.4 ZEPHYR/4.4.0",
        )

    def test_dfu_version_is_monotonic_for_ncs_patch_releases(self) -> None:
        self.assertLess(dfu_application_version("3.3.4"), dfu_application_version("3.3.5"))


if __name__ == "__main__":
    unittest.main()
