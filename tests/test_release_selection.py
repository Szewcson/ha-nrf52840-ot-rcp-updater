from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.main import (
    _clear_selected_target,
    _mark_installed_unknown,
    _prepare_state,
    _rescan_installed_state,
    _return_to_automatic_target,
    _select_release,
    _selected_target,
    _set_selected_target,
)
from app.manifest import FirmwareManifest, ManifestError
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
            "selected_ncs_version": "3.3.4",
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            updated = _mark_installed_unknown(store, state)

            self.assertNotIn("installed", updated)
            self.assertNotIn("verified_at", updated)
            self.assertEqual(updated["selected_ncs_version"], "3.3.4")
            self.assertEqual(store.load(), updated)

    def test_prepare_state_discards_retired_dfu_activity_timestamp(self) -> None:
        state = {
            "dfu_activity_at": "2026-09-05T08:30:00+00:00",
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.5.0-preview1",
                "zephyr_version": "4.4.0",
            },
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            store.save(state)
            updated = _prepare_state(store, _settings())
            self.assertEqual(store.load(), updated)

        self.assertNotIn("dfu_activity_at", updated)
        self.assertEqual(updated["installed"], state["installed"])

    def test_startup_rescan_replaces_stale_version_with_live_spinel_version(self) -> None:
        class Updater:
            @staticmethod
            def current_version() -> NcpVersion:
                return NcpVersion(
                    raw="OPENTHREAD/test; HW/PCA10059 NCS/3.3.4 ZEPHYR/4.3.99",
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

    def test_verified_rcp_uses_the_newest_release(self) -> None:
        release = _select_release(
            _manifest(),
            _settings(),
            {
                "installed": {
                    "hardware": "PCA10059",
                    "ncs_version": "3.3.4",
                    "zephyr_version": "4.4.0",
                }
            },
        )
        self.assertEqual(release.ncs_version, "3.4.0")

    def test_prerelease_policy_and_minor_pin_filter_the_available_release(self) -> None:
        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.4.0",
                "zephyr_version": "4.4.0",
            }
        }
        release = _select_release(_manifest(), _settings(allow_prereleases=True), state)
        self.assertEqual(release.ncs_version, "3.5.0-rc1")

        with self.assertRaisesRegex(ManifestError, "would downgrade"):
            _select_release(_manifest(), _settings(pinned_minor="3.3"), state)

    def test_manual_target_allows_a_selected_downgrade(self) -> None:
        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.4.0",
                "zephyr_version": "4.4.0",
            }
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            selected = _set_selected_target(store, state, _manifest(), _settings(), "3.3.4")
            release = _select_release(_manifest(), _settings(), selected)

        self.assertEqual(release.ncs_version, "3.3.4")

    def test_verified_manual_target_immediately_returns_to_normal_policy(self) -> None:
        state = {
            "installed": {
                "hardware": "PCA10059",
                "ncs_version": "3.3.4",
                "zephyr_version": "4.4.0",
            },
        }

        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            _set_selected_target(store, state, _manifest(), _settings(), "3.3.4")
            state, release = _return_to_automatic_target(
                store, _manifest(), _settings()
            )

        self.assertEqual(release.ncs_version, "3.4.0")
        self.assertIsNone(_selected_target(state))

    def test_runtime_target_selection_stores_a_manual_target(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            settings = _settings()
            state = _set_selected_target(store, {}, _manifest(), settings, "3.3.4")

            self.assertEqual(_selected_target(state), "3.3.4")
            self.assertEqual(_select_release(_manifest(), settings, state).ncs_version, "3.3.4")
            self.assertIsNone(_selected_target(_clear_selected_target(store)))


if __name__ == "__main__":
    unittest.main()
