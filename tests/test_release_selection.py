from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.main import (
    _mark_installed_unknown,
    _policy_target_is_downgrade,
    _prepare_state,
    _rescan_installed_state,
    _select_release,
)
from app.manifest import FirmwareManifest
from app.models import NcpVersion, Settings
from app.spinel import SpinelError
from app.state import StateStore
from app.updater import RescanDeferred


def _settings(
    *, allow_prereleases: bool = False, pinned_minor: str | None = None
) -> Settings:
    return Settings.from_mapping(
        {
            "device": "/dev/serial/by-id/nrf52840",
            "baudrate": 460800,
            "safe_update": True,
            "allow_legacy_rcp": True,
            "allow_prereleases": allow_prereleases,
            "pinned_ncs_minor": pinned_minor,
            "manifest_poll_interval": 3600,
            "idle_window": 20,
            "boot_timeout": 45,
        }
    )


def _manifest() -> FirmwareManifest:
    return FirmwareManifest.from_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "releases": [
                    {
                        "hardware": "PCA10059",
                        "ncs_version": version,
                        "zephyr_version": "4.4.0",
                        "dfu_application_version": 3_004_000,
                        "artifact": {
                            "url": f"https://example.invalid/{version}.elf",
                            "sha256": "0" * 64,
                            "filename": f"{version}.elf",
                            "signature_url": f"https://example.invalid/{version}.elf.sig",
                        },
                        "release_url": "https://example.invalid/release",
                        "release_summary": "Test release",
                    }
                    for version in ("3.3.4", "3.4.0", "3.5.0-preview1", "3.5.0-rc1")
                ],
            }
        ).encode()
    )


class ReleaseSelectionTests(unittest.TestCase):
    def test_failed_install_clears_the_persisted_installed_version(self) -> None:
        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.5.0-preview1",
                "zephyr_version": "4.4.0",
            },
            "verified_at": "2026-09-04T00:00:00+00:00",
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            updated = _mark_installed_unknown(store, state)

            self.assertNotIn("installed", updated)
            self.assertNotIn("verified_at", updated)
            self.assertEqual(store.load(), updated)

    def test_prepare_state_discards_retired_mqtt_target_and_activity(self) -> None:
        state = {
            "dfu_activity_at": "2026-09-05T08:30:00+00:00",
            "selected_ncs_version": "3.5.0-preview1",
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.3.4",
                "zephyr_version": "4.3.99",
            },
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            store.save(state)
            updated = _prepare_state(store, _settings())

            self.assertNotIn("dfu_activity_at", updated)
            self.assertNotIn("selected_ncs_version", updated)
            self.assertEqual(store.load(), updated)

    def test_startup_rescan_replaces_stale_version_with_live_spinel_version(self) -> None:
        class Updater:
            @staticmethod
            def current_version() -> NcpVersion:
                return NcpVersion(
                    raw="OPENTHREAD/test; NRF52840 PCA10059 N/3.3.4 Z/4.3.99",
                    hardware="PCA10059",
                    ncs_version="3.3.4",
                    zephyr_version="4.3.99",
                )

        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.5.0-preview1",
                "zephyr_version": "4.4.0",
            }
        }
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            updated, completed = _rescan_installed_state(store, state, _settings(), Updater())

            self.assertTrue(completed)
            self.assertEqual(updated["installed"]["ncs_version"], "3.3.4")
            self.assertEqual(store.load(), updated)

    def test_startup_rescan_marks_the_version_unknown_when_spinel_fails(self) -> None:
        class Updater:
            @staticmethod
            def current_version() -> NcpVersion:
                raise SpinelError("RCP is in Secure DFU mode")

        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.5.0-preview1",
                "zephyr_version": "4.4.0",
            }
        }
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            updated, completed = _rescan_installed_state(store, state, _settings(), Updater())

            self.assertTrue(completed)
            self.assertEqual(updated, {})
            self.assertEqual(store.load(), {})

    def test_startup_rescan_retains_state_while_otbr_is_not_ready(self) -> None:
        class Updater:
            @staticmethod
            def current_version() -> NcpVersion:
                raise RescanDeferred("OTBR is starting")

        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.3.4",
                "zephyr_version": "4.3.99",
            }
        }
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            store.save(state)
            updated, completed = _rescan_installed_state(store, state, _settings(), Updater())

            self.assertFalse(completed)
            self.assertEqual(updated, state)
            self.assertEqual(store.load(), state)

    def test_legacy_rcp_uses_the_newest_release_from_its_channel(self) -> None:
        release = _select_release(_manifest(), _settings(), {})
        self.assertEqual(release.ncs_version, "3.4.0")

    def test_prerelease_policy_and_minor_pin_can_request_an_explicit_downgrade(self) -> None:
        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.4.0",
                "zephyr_version": "4.4.0",
            }
        }
        release = _select_release(_manifest(), _settings(allow_prereleases=True), state)
        self.assertEqual(release.ncs_version, "3.5.0-rc1")

        pinned_release = _select_release(_manifest(), _settings(pinned_minor="3.3"), state)
        self.assertEqual(pinned_release.ncs_version, "3.3.4")
        self.assertTrue(_policy_target_is_downgrade(pinned_release, state))
        self.assertFalse(_policy_target_is_downgrade(release, state))


if __name__ == "__main__":
    unittest.main()
