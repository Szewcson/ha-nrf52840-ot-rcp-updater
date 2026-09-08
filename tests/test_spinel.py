from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.spinel import HdlcDecoder, hdlc_encode, pack_uint, parse_ncp_version, unpack_uint


class SpinelTests(unittest.TestCase):
    def test_hdlc_round_trip_escapes_control_bytes(self) -> None:
        payload = bytes((0x81, 0x7E, 0x7D, 0x11, 0x13, 0x02))
        encoded = hdlc_encode(payload)
        self.assertEqual(list(HdlcDecoder().feed(encoded)), [payload])

    def test_packed_uint_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 16_384, 0xFFFFFFFF):
            encoded = pack_uint(value)
            self.assertEqual(unpack_uint(encoded, 0), (value, len(encoded)))

    def test_parses_additive_platform_info_after_the_conventional_soc_token(self) -> None:
        version = parse_ncp_version(
            "OPENTHREAD/1.4.0; NRF52840 PCA10059 N/3.3.4 Z/4.4.0; "
            "Sep 2 2026 12:00:00"
        )
        self.assertEqual(version.hardware, "PCA10059")
        self.assertEqual(version.ncs_version, "3.3.4")
        self.assertEqual(version.zephyr_version, "4.4.0")

    def test_parses_nordic_prerelease_platform_info(self) -> None:
        version = parse_ncp_version(
            "OPENTHREAD/1.4.0; NRF52840 PCA10059 N/3.5.0-preview1 Z/4.4.0"
        )

        self.assertEqual(version.ncs_version, "3.5.0-preview1")

    def test_keeps_parsing_the_legacy_platform_info_without_a_soc_token(self) -> None:
        version = parse_ncp_version("OPENTHREAD/1.4.0; HW/PCA10059 NCS/3.3.4 ZEPHYR/4.4.0")

        self.assertEqual(version.hardware, "PCA10059")
        self.assertEqual(version.ncs_version, "3.3.4")
        self.assertEqual(version.zephyr_version, "4.4.0")


if __name__ == "__main__":
    unittest.main()
