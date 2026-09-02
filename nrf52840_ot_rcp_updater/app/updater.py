"""Transactional OpenThread RCP firmware updates."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from .dfu import NordicDfuFlasher
from .manifest import FirmwareManifest, download_artifact
from .models import FirmwareRelease, NcpVersion, Settings
from .preflight import OtbrRestClient
from .spinel import SpinelClient, SpinelError
from .state import StateStore, update_lock
from .supervisor import SupervisorClient


class UpdateError(RuntimeError):
    """An RCP update was rejected or did not complete verification."""


class RcpUpdater:
    """Coordinates the only supported OTBR -> DFU -> OTBR ownership transition."""

    def __init__(
        self,
        settings: Settings,
        state_store: StateStore,
        supervisor: SupervisorClient | None = None,
        flasher: NordicDfuFlasher | None = None,
    ) -> None:
        self._settings = settings
        self._state_store = state_store
        self._supervisor = supervisor or SupervisorClient()
        self._flasher = flasher or NordicDfuFlasher()

    def current_version(self) -> NcpVersion:
        """Read the NCP only while OTBR is stopped, then promptly restore it."""

        with update_lock(self._state_directory):
            with self._supervisor.temporarily_stop(self._settings.otbr_addon_slug):
                version = self._spinel().get_ncp_version()
        return version

    def install(self, release: FirmwareRelease) -> NcpVersion:
        """Install a verified release and persist state only after NCP verification."""

        self._validate_release(release)
        package = download_artifact(release, self._state_directory / "downloads")
        if self._settings.safe_update:
            self._require_quiet_management_window()

        with update_lock(self._state_directory):
            with self._supervisor.temporarily_stop(self._settings.otbr_addon_slug):
                before = self._spinel().get_ncp_version()
                self._validate_current_ncp(before)
                if self._matches_release(before, release):
                    after = before
                else:
                    if not self._settings.dfu_serial_number:
                        raise UpdateError(
                            "dfu_serial_number is required to prevent flashing a different Nordic device"
                        )
                    self._spinel().reset_bootloader()
                    self._flasher.flash(package, self._settings.dfu_serial_number)
                    after = self._wait_for_expected_release(release)

        if self._settings.safe_update:
            self._require_otbr_healthy()
        self._state_store.save(
            {
                "installed": asdict(after),
                "verified_at": datetime.now(UTC).isoformat(),
            }
        )
        return after

    @property
    def _state_directory(self) -> Path:
        return self._state_store.directory

    def _spinel(self) -> SpinelClient:
        return SpinelClient(str(self._settings.device), self._settings.baudrate)

    def _validate_release(self, release: FirmwareRelease) -> None:
        if release.hardware != self._settings.hardware:
            raise UpdateError(
                f"release hardware {release.hardware} does not match configured {self._settings.hardware}"
            )

    def _validate_current_ncp(self, version: NcpVersion) -> None:
        if (
            any(value is None for value in (version.hardware, version.ncs_version, version.zephyr_version))
            and not self._settings.allow_legacy_rcp
        ):
            raise UpdateError(
                "RCP has incomplete HW/NCS/ZEPHYR tags; set allow_legacy_rcp only after confirming its hardware"
            )
        if version.hardware is not None and version.hardware != self._settings.hardware:
            raise UpdateError(
                f"connected RCP hardware {version.hardware} does not match {self._settings.hardware}"
            )

    def _wait_for_expected_release(self, release: FirmwareRelease) -> NcpVersion:
        deadline = monotonic() + self._settings.boot_timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                version = self._spinel().get_ncp_version()
                self._verify_release(version, release)
                return version
            except (SpinelError, UpdateError) as err:
                last_error = err
                sleep(1)
        raise UpdateError(f"RCP did not report the expected firmware after DFU: {last_error}")

    @staticmethod
    def _verify_release(version: NcpVersion, release: FirmwareRelease) -> None:
        expected = {
            "hardware": release.hardware,
            "ncs_version": release.ncs_version,
            "zephyr_version": release.zephyr_version,
        }
        actual = {
            "hardware": version.hardware,
            "ncs_version": version.ncs_version,
            "zephyr_version": version.zephyr_version,
        }
        mismatches = [
            f"{field} is {actual[field]!r}, expected {expected[field]!r}"
            for field in expected
            if actual[field] != expected[field]
        ]
        if mismatches:
            raise UpdateError("RCP version tags did not match manifest: " + "; ".join(mismatches))

    @staticmethod
    def _matches_release(version: NcpVersion, release: FirmwareRelease) -> bool:
        return (
            version.hardware == release.hardware
            and version.ncs_version == release.ncs_version
            and version.zephyr_version == release.zephyr_version
        )

    def _require_quiet_management_window(self) -> None:
        if not self._settings.otbr_api_url:
            raise UpdateError("safe_update requires otbr_api_url for OTBR management preflight")
        OtbrRestClient(self._settings.otbr_api_url).require_quiet_management_window(
            self._settings.idle_window
        )

    def _require_otbr_healthy(self) -> None:
        if not self._settings.otbr_api_url:
            raise UpdateError("safe_update requires otbr_api_url for OTBR health verification")
        client = OtbrRestClient(self._settings.otbr_api_url)
        deadline = monotonic() + self._settings.boot_timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                client.require_healthy()
                return
            except Exception as err:  # The public API returns several transport error types.
                last_error = err
                sleep(1)
        raise UpdateError(f"OTBR did not become healthy after the RCP update: {last_error}")
