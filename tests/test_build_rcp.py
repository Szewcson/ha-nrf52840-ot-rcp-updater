from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.build_rcp import (
    BuildError,
    _read_ncs_version,
    _validate_platform_info,
    _validate_platform_info_binary,
    _validate_platform_info_value,
    dfu_application_version,
    platform_info,
)


class BuildRcpTests(unittest.TestCase):
    def test_platform_info_leads_with_the_conventional_nordic_soc_token(self) -> None:
        self.assertEqual(
            platform_info("3.3.4", "4.4.0"),
            "NRF52840 PCA10059 N/3.3.4 Z/4.4.0",
        )

    def test_platform_info_rejects_non_ascii_and_reserved_delimiters(self) -> None:
        for value in ("NRF52840; HW/PCA10059", "NRF52840_HW/PCA10059", "NRF52840 caf\u00e9"):
            with self.subTest(value=value), self.assertRaises(BuildError):
                _validate_platform_info_value(value)

    def test_dfu_version_is_monotonic_for_ncs_patch_releases(self) -> None:
        self.assertLess(dfu_application_version("3.3.4"), dfu_application_version("3.3.5"))

    def test_dfu_version_is_monotonic_across_a_minor_after_high_patch(self) -> None:
        self.assertLess(
            dfu_application_version("3.4.999"), dfu_application_version("3.5.0-preview1")
        )

    def test_dfu_version_orders_preview_rc_and_final(self) -> None:
        self.assertLess(
            dfu_application_version("3.5.0-preview1"), dfu_application_version("3.5.0-rc1")
        )
        self.assertLess(dfu_application_version("3.5.0-rc1"), dfu_application_version("3.5.0"))

    def test_reads_ncs_v340_structured_version_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            version_file = root / "nrf" / "VERSION"
            version_file.parent.mkdir()
            version_file.write_text(
                "VERSION_MAJOR = 3\n"
                "VERSION_MINOR = 4\n"
                "PATCHLEVEL = 0\n"
                "VERSION_TWEAK = 0\n"
                "EXTRAVERSION =\n"
                "VERSION_METADATA = lts\n",
                encoding="utf-8",
            )

            self.assertEqual(_read_ncs_version(root), "3.4.0")

    def test_reads_current_root_version_file_with_prerelease_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text(
                "VERSION_MAJOR = 3\nVERSION_MINOR = 5\nPATCHLEVEL = 0\nEXTRAVERSION = preview1\n",
                encoding="utf-8",
            )

            self.assertEqual(_read_ncs_version(root), "3.5.0-preview1")

    def test_validates_platform_info_from_the_sysbuild_rcp_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            build_directory = Path(directory)
            config = build_directory / "coprocessor" / "zephyr" / ".config"
            config.parent.mkdir(parents=True)
            config.write_text(
                'CONFIG_OPENTHREAD_CONFIG_PLATFORM_INFO="NRF52840 PCA10059 N/3.3.4 Z/4.4.0"\n',
                encoding="utf-8",
            )

            _validate_platform_info(
                build_directory, "NRF52840 PCA10059 N/3.3.4 Z/4.4.0"
            )

    def test_validates_the_full_composed_version_from_the_compiled_rcp_image(self) -> None:
        with TemporaryDirectory() as directory:
            image = Path(directory) / "zephyr.bin"
            info = "NRF52840 PCA10059 N/3.4.0 Z/4.4.0"
            version = f"OPENTHREAD/ncs-thread-reference-20250402; {info}; Sep 2 2026 12:00:00"
            image.write_bytes(b"\x02\x04\x05\x06\x07" + version.encode("ascii") + b"\0suffix")

            self.assertEqual(_validate_platform_info_binary(image, info), version)

    def test_rejects_a_composed_version_longer_than_the_host_spinel_buffer(self) -> None:
        with TemporaryDirectory() as directory:
            image = Path(directory) / "zephyr.bin"
            info = "NRF52840 PCA10059 N/3.4.0 Z/4.4.0"
            version = "OPENTHREAD/" + "x" * 80 + f"; {info}; Sep 2 2026 12:00:00"
            self.assertGreater(len(version.encode("ascii")), 127)
            image.write_bytes(version.encode("ascii") + b"\0")

            with self.assertRaisesRegex(BuildError, "127 bytes"):
                _validate_platform_info_binary(image, info)

    def test_accepts_a_composed_version_at_the_127_byte_host_limit(self) -> None:
        with TemporaryDirectory() as directory:
            image = Path(directory) / "zephyr.bin"
            info = "NRF52840 PCA10059 N/3.4.0 Z/4.4.0"
            fixed = f"; {info}; Sep 2 2026 12:00:00"
            version = "OPENTHREAD/" + "x" * (127 - len("OPENTHREAD/") - len(fixed)) + fixed
            self.assertEqual(len(version.encode("ascii")), 127)
            image.write_bytes(version.encode("ascii") + b"\0")

            self.assertEqual(_validate_platform_info_binary(image, info), version)

    def test_compact_tags_leave_room_for_a_previously_overlong_ncp_version(self) -> None:
        legacy_info = "NRF52840 HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0"
        compact_info = platform_info("3.4.0", "4.4.0")
        suffix = "; Sep 2 2026 12:00:00"
        package_prefix = "OPENTHREAD/"
        package = package_prefix + "x" * (
            130 - len(package_prefix) - len(legacy_info) - len(suffix) - 2
        )
        legacy_version = f"{package}; {legacy_info}{suffix}"
        compact_version = f"{package}; {compact_info}{suffix}"

        self.assertEqual(len(legacy_version.encode("ascii")), 130)
        self.assertLessEqual(len(compact_version.encode("ascii")), 127)
        self.assertEqual(len(legacy_version) - len(compact_version), 10)


if __name__ == "__main__":
    unittest.main()
