"""Transactional OpenThread RCP firmware updates."""

from __future__ import annotations

import logging
from hashlib import sha256
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from .dfu import DfuError, DfuSelector, DfuTransferError, NrfDfuFlasher
from .manifest import MAX_ARTIFACT_BYTES, ManifestError, download_artifact, validate_rcp_elf
from .models import (
    CORE_OTBR_ADDON_SLUG,
    CORE_OTBR_API_URL,
    DEFAULT_DFU_VID_PID,
    SUPPORTED_HARDWARE,
    FirmwareRelease,
    NcpVersion,
    PreparedFirmware,
    Settings,
    ValidationError,
    version_key,
)
from .preflight import OtbrRestClient, PreflightError
from .spinel import SpinelClient, SpinelError
from .state import StateStore, update_lock
from .supervisor import SupervisorClient, SupervisorError


class UpdateError(RuntimeError):
    """An RCP update was rejected or did not complete verification."""


class RescanDeferred(UpdateError):
    """OTBR is not yet in a safe state for a non-invasive version rescan."""


_MIN_POST_DFU_BOOT_TIMEOUT = 90
# Four QEMU host-libusb autoscan intervals. This is deliberately opt-in because
# it mitigates direct USB passthrough timing, not an RCP firmware requirement.
_QEMU_USB_REENUMERATION_SETTLE_SECONDS = 8
LOGGER = logging.getLogger(__name__)
# nrfdfu does not expose byte-level progress.  Keep the optional percentage for
# a future backend that does, but report only an honest phase for this backend.
ProgressReporter = Callable[[int | None, str], None]


class RcpUpdater:
    """Coordinates the only supported OTBR -> DFU -> OTBR ownership transition."""

    def __init__(
        self,
        settings: Settings,
        state_store: StateStore,
        supervisor: SupervisorClient | None = None,
        flasher: NrfDfuFlasher | None = None,
    ) -> None:
        self._settings = settings
        self._state_store = state_store
        self._supervisor = supervisor or SupervisorClient()
        self._flasher = flasher or NrfDfuFlasher()

    def current_version(self) -> NcpVersion:
        """Read the NCP only after OTBR reaches a stable, quiet ownership state."""

        with update_lock(self._state_directory):
            try:
                otbr_was_running = self._prepare_safe_update()
                with self._supervisor.temporarily_stop(CORE_OTBR_ADDON_SLUG) as was_running:
                    if self._settings.safe_update and was_running != otbr_was_running:
                        raise RescanDeferred("OTBR changed state before the version rescan")
                    return self._spinel().get_ncp_version()
            except RescanDeferred:
                raise
            except (PreflightError, SupervisorError, UpdateError) as err:
                raise RescanDeferred(f"OTBR is not ready for a version rescan: {err}") from err

    def install(
        self,
        release: FirmwareRelease,
        selected_target: bool = False,
        progress: ProgressReporter | None = None,
    ) -> NcpVersion:
        """Install a signed release through the shared prepared-artifact path."""

        self._report_progress(progress, None, "Validating release")
        self._validate_release(release)
        self._report_progress(progress, None, "Downloading firmware")
        package = self.prepare_release(release)
        return self.install_prepared(
            package, selected_target=selected_target, progress=progress, report_validation=False
        )

    def prepare_release(self, release: FirmwareRelease) -> PreparedFirmware:
        """Download and authenticate a manifest release before touching OTBR."""

        self._validate_release(release)
        package = download_artifact(release, self._state_directory / "downloads")
        try:
            data = package.read_bytes()
        except OSError as err:  # pragma: no cover - download_artifact guarantees this.
            raise UpdateError("verified firmware artifact disappeared") from err
        return PreparedFirmware(
            path=package,
            hardware=release.hardware,
            ncs_version=release.ncs_version,
            zephyr_version=release.zephyr_version,
            dfu_application_version=release.dfu_application_version,
            sha256=sha256(data).hexdigest(),
            size=len(data),
            source="release",
            release=release,
        )

    def preflight(self, package: PreparedFirmware) -> dict[str, object]:
        """Return only checks that can be performed without disrupting the RCP."""

        self._validate_prepared(package)
        dfu_backend_available = self._flasher.backend_available()
        rcp_device_present = self._settings.device.exists()
        checks: list[dict[str, str]] = [
            {
                "name": "firmware artifact",
                "state": "ok",
                "message": "Bounded PCA10059 ARM ELF validated",
            },
            {
                "name": "firmware target",
                "state": "ok",
                "message": f"Embedded hardware tag is {package.hardware}",
            },
            {
                "name": "DFU backend",
                "state": "ok" if dfu_backend_available else "error",
                "message": (
                    "nrfdfu is available"
                    if dfu_backend_available
                    else "nrfdfu is unavailable in the app image"
                ),
            },
        ]
        if rcp_device_present:
            checks.extend(
                [
                    {
                        "name": "RCP serial device",
                        "state": "ok",
                        "message": f"Configured device {self._settings.device} is present",
                    },
                    {
                        "name": "bootloader transition",
                        "state": "pending",
                        "message": "Verified during the protected OTBR-to-DFU handoff",
                    },
                ]
            )
        elif not dfu_backend_available:
            checks.extend(
                [
                    {
                        "name": "RCP serial device",
                        "state": "error",
                        "message": (
                            f"Configured device {self._settings.device} is absent and "
                            "nrfdfu cannot check Secure DFU recovery"
                        ),
                    },
                    {
                        "name": "bootloader transition",
                        "state": "error",
                        "message": "nrfdfu is unavailable in the app image",
                    },
                ]
            )
        else:
            # An intentional manual target may recover a dongle that is already
            # in Secure DFU. Probe with the same constrained selector that the
            # transaction will use; never turn a missing normal tty into a broad
            # VID:PID match.
            try:
                selector = self._flasher.bootloader_selector(
                    self._settings.dfu_serial_number,
                    self._settings.dfu_usb_path,
                )
                recovery_target = self._flasher.probe_dfu_target(selector)
            except DfuError as err:
                recovery_target = None
                recovery_error = str(err)
            else:
                recovery_error = recovery_target.diagnostic
            if recovery_target is not None and recovery_target.ready:
                checks.extend(
                    [
                        {
                            "name": "RCP serial device",
                            "state": "ok",
                            "message": (
                                "Normal RCP serial device is absent; an exact Secure DFU target "
                                "is ready for recovery"
                            ),
                        },
                        {
                            "name": "bootloader transition",
                            "state": "ok",
                            "message": "Exact Secure DFU target was confirmed",
                        },
                    ]
                )
            else:
                checks.extend(
                    [
                        {
                            "name": "RCP serial device",
                            "state": "error",
                            "message": (
                                f"Configured device {self._settings.device} is absent; "
                                f"Secure DFU recovery is unavailable: {recovery_error}"
                            ),
                        },
                        {
                            "name": "bootloader transition",
                            "state": "error",
                            "message": "Exact Secure DFU target could not be confirmed",
                        },
                    ]
                )
        try:
            otbr_state = self._supervisor.addon_state(CORE_OTBR_ADDON_SLUG)
        except SupervisorError as err:
            checks.append(
                {"name": "OTBR Supervisor", "state": "error", "message": str(err)}
            )
        else:
            state = "ok" if otbr_state in {"started", "running", "stopped", "not_running"} else "error"
            checks.append(
                {
                    "name": "OTBR Supervisor",
                    "state": state,
                    "message": f"OTBR state is {otbr_state}",
                }
            )
            if self._settings.safe_update and otbr_state in {"started", "running"}:
                try:
                    actions = OtbrRestClient(CORE_OTBR_API_URL).actions()
                except PreflightError as err:
                    checks.append(
                        {"name": "OTBR REST", "state": "error", "message": str(err)}
                    )
                else:
                    checks.append(
                        {
                            "name": "OTBR REST",
                            "state": "ok",
                            "message": f"OTBR management API responded with {len(actions)} action(s)",
                        }
                    )
        ready = all(check["state"] != "error" for check in checks)
        return {"ready": ready, "checks": checks}

    def install_prepared(
        self,
        package: PreparedFirmware,
        selected_target: bool = False,
        progress: ProgressReporter | None = None,
        report_validation: bool = True,
    ) -> NcpVersion:
        """Install one prevalidated ELF using the only OTBR/DFU transaction."""

        if report_validation:
            self._report_progress(progress, None, "Validating firmware")
        self._validate_prepared(package)

        with update_lock(self._state_directory):
            # If the normal device has already vanished, prove that a permitted
            # Secure DFU target exists before disrupting OTBR. This keeps a stale
            # MQTT command from turning a missing USB device into an OTBR outage.
            preselected_dfu_selector = self._bootloader_selector_if_rcp_is_missing(
                selected_target
            )
            self._report_progress(progress, None, "Preparing OTBR and RCP")
            otbr_was_running = self._prepare_safe_update()
            with self._supervisor.temporarily_stop(CORE_OTBR_ADDON_SLUG) as was_running:
                if self._settings.safe_update and was_running != otbr_was_running:
                    raise UpdateError(
                        "OTBR state changed during the safe-update preflight; retry the update"
                    )
                if preselected_dfu_selector is not None:
                    before = None
                    dfu_selector = preselected_dfu_selector
                else:
                    try:
                        before = self._spinel().get_ncp_version()
                    except SpinelError as err:
                        try:
                            dfu_selector = self._bootloader_recovery_selector(
                                "RCP did not answer Spinel", selected_target
                            )
                        except UpdateError as recovery_error:
                            raise recovery_error from err
                        before = None
                    else:
                        dfu_selector = None

                if before is not None:
                    self._validate_current_ncp(before, package, selected_target)
                if before is not None and self._matches_package(before, package):
                    self._report_progress(progress, None, "Firmware already matches target")
                    after = before
                else:
                    if before is None:
                        # The device is already a verified DFU target; do not reset it again.
                        assert dfu_selector is not None
                    else:
                        try:
                            dfu_selector = self._flasher.selector_for_device(
                                self._settings.device,
                                self._settings.dfu_serial_number,
                            )
                        except DfuError as err:
                            raise UpdateError(str(err)) from err
                        self._report_progress(progress, None, "Entering Secure DFU")
                        self._spinel().reset_bootloader()
                        self._settle_qemu_usb_reenumeration(progress, "Secure DFU")
                    if before is None:
                        self._report_progress(progress, None, "Using detected Secure DFU")
                    self._report_progress(progress, None, "Flashing firmware")
                    try:
                        self._flasher.flash(
                            package.path, dfu_selector, package.dfu_application_version
                        )
                    except DfuTransferError as err:
                        # USB can disconnect while the bootloader finishes an accepted image.
                        # Treat only a live Spinel match as evidence of a successful update.
                        LOGGER.warning("nrfdfu failed; reconciling the RCP firmware over Spinel")
                        self._settle_qemu_usb_reenumeration(progress, "RCP")
                        self._report_progress(progress, None, "Verifying RCP firmware")
                        try:
                            after = self._wait_for_expected_release(
                                package, dfu_selector, progress=progress
                            )
                        except UpdateError as verification_error:
                            raise UpdateError(
                                f"{err}; RCP did not verify the requested firmware: "
                                f"{verification_error}"
                            ) from err
                    else:
                        self._settle_qemu_usb_reenumeration(progress, "RCP")
                        self._report_progress(progress, None, "Verifying RCP firmware")
                        after = self._wait_for_expected_release(
                            package, dfu_selector, progress=progress
                        )

            # Do not release the inter-process transaction lock until OTBR has
            # recovered and the verified result is durable. A process restart
            # or a second command must not overlap this final ownership phase.
            if self._settings.safe_update and otbr_was_running:
                self._report_progress(progress, None, "Waiting for OTBR")
                self._require_otbr_healthy()
            state = self._state_store.load()
            state.update(
                {
                    "installed": asdict(after),
                    "verified_at": datetime.now(UTC).isoformat(),
                }
            )
            self._state_store.save(state)
            self._report_progress(progress, 100, "Update verified")
            return after

    @property
    def _state_directory(self) -> Path:
        return self._state_store.directory

    def _spinel(self) -> SpinelClient:
        return SpinelClient(str(self._settings.device), self._settings.baudrate)

    @staticmethod
    def _report_progress(
        progress: ProgressReporter | None, update_percentage: int | None, stage: str
    ) -> None:
        """Keep telemetry failures from interrupting the safety-critical transaction."""

        if progress is None:
            return
        try:
            progress(update_percentage, stage)
        except Exception:
            LOGGER.exception("Unable to publish RCP update progress")

    def _settle_qemu_usb_reenumeration(
        self, progress: ProgressReporter | None, personality: str
    ) -> None:
        """Avoid an early guest probe while QEMU replaces a direct USB device.

        QEMU host-libusb detects a vanished device only when guest I/O gets
        ``NO_DEVICE``. Waiting until the replacement personality is ready lets
        that first probe cause an immediate successful reattach instead of an
        early failed open. This is an opt-in virtualization workaround.
        """

        if not self._settings.qemu_usb_reenumeration_workaround:
            return
        LOGGER.info(
            "QEMU USB re-enumeration workaround: waiting %s seconds for %s",
            _QEMU_USB_REENUMERATION_SETTLE_SECONDS,
            personality,
        )
        self._report_progress(
            progress,
            None,
            f"Waiting for {personality} USB re-enumeration",
        )
        sleep(_QEMU_USB_REENUMERATION_SETTLE_SECONDS)

    def diagnostics(self) -> dict[str, object]:
        """Return non-invasive device and OTBR observations for Home Assistant."""

        diagnostics: dict[str, object] = {
            "configured_rcp_device": str(self._settings.device),
            "configured_rcp_device_present": self._settings.device.exists(),
            "configured_dfu_vid_pid": DEFAULT_DFU_VID_PID,
            "configured_dfu_serial": self._settings.dfu_serial_number,
            "configured_dfu_usb_path": self._settings.dfu_usb_path,
            "otbr_addon_slug": CORE_OTBR_ADDON_SLUG,
            "otbr_api_url": CORE_OTBR_API_URL,
        }
        try:
            diagnostics["detected_rcp_usb_serial"] = self._flasher.normal_usb_serial(
                self._settings.device
            )
        except DfuError:
            diagnostics["detected_rcp_usb_serial"] = None
        diagnostics["detected_rcp_usb_vid_pid"] = self._flasher.normal_usb_vid_pid(
            self._settings.device
        )
        try:
            diagnostics["otbr_state"] = self._supervisor.addon_state(CORE_OTBR_ADDON_SLUG)
        except SupervisorError as err:
            diagnostics["otbr_state"] = "unavailable"
            diagnostics["otbr_error"] = str(err)
        try:
            selector = self._flasher.selector_for_device(
                self._settings.device,
                self._settings.dfu_serial_number,
            )
            diagnostics["rcp_usb_topology_known"] = selector.physical_path is not None
        except DfuError:
            selector = self._flasher.bootloader_selector(
                self._settings.dfu_serial_number,
                self._settings.dfu_usb_path,
            )
            diagnostics["rcp_usb_topology_known"] = False
        try:
            target = self._flasher.probe_dfu_target(selector)
            diagnostics["dfu_target_present"] = target.present
            diagnostics["dfu_target_ready"] = target.ready
            diagnostics["resolved_dfu_serial"] = (
                target.target.serial_number if target.target is not None else None
            )
            if not target.ready:
                diagnostics["dfu_probe_output"] = target.diagnostic or "no diagnostic output"
        except DfuError as err:
            diagnostics["dfu_target_present"] = False
            diagnostics["dfu_target_ready"] = False
            diagnostics["resolved_dfu_serial"] = None
            diagnostics["dfu_probe_error"] = str(err)
        return diagnostics

    def _validate_release(self, release: FirmwareRelease) -> None:
        if release.hardware != SUPPORTED_HARDWARE:
            raise UpdateError(
                f"release hardware {release.hardware} does not match supported {SUPPORTED_HARDWARE}"
            )

    @staticmethod
    def _validate_prepared(package: PreparedFirmware) -> None:
        if package.hardware != SUPPORTED_HARDWARE:
            raise UpdateError(
                f"firmware hardware {package.hardware} does not match supported {SUPPORTED_HARDWARE}"
            )
        try:
            size = package.path.stat().st_size
            if size != package.size or not 1 <= size <= MAX_ARTIFACT_BYTES:
                raise UpdateError("validated firmware artifact changed before flashing")
            data = package.path.read_bytes()
        except OSError as err:
            raise UpdateError("validated firmware artifact is unavailable") from err
        if sha256(data).hexdigest() != package.sha256:
            raise UpdateError("validated firmware artifact changed before flashing")
        try:
            validate_rcp_elf(data)
        except ManifestError as err:
            raise UpdateError(str(err)) from err
        if package.release is not None and (
            package.release.hardware != package.hardware
            or package.release.ncs_version != package.ncs_version
            or package.release.zephyr_version != package.zephyr_version
            or package.release.dfu_application_version != package.dfu_application_version
        ):
            raise UpdateError("release metadata does not match its prepared firmware artifact")

    def _validate_current_ncp(
        self, version: NcpVersion, package: PreparedFirmware, selected_target: bool = False
    ) -> None:
        identity = (version.hardware, version.ncs_version, version.zephyr_version)
        legacy = any(value is None for value in identity)
        if legacy and not self._settings.allow_legacy_rcp:
            raise UpdateError(
                "RCP has incomplete HW/NCS/ZEPHYR tags; set allow_legacy_rcp only after confirming its hardware"
            )
        if version.hardware is not None and version.hardware != SUPPORTED_HARDWARE:
            raise UpdateError(
                f"connected RCP hardware {version.hardware} does not match {SUPPORTED_HARDWARE}"
            )
        if not legacy:
            assert version.ncs_version is not None
            try:
                downgrade = version_key(package.ncs_version) < version_key(version.ncs_version)
            except ValidationError as err:
                raise UpdateError("connected RCP reported an invalid NCS version tag") from err
            if (
                downgrade
                and not selected_target
            ):
                raise UpdateError(
                    "RCP downgrade requires selecting the exact manual firmware target"
                )

    def _bootloader_selector_if_rcp_is_missing(self, selected_target: bool) -> DfuSelector | None:
        """Verify bootloader recovery before stopping OTBR for a missing RCP path."""

        if self._settings.device.exists():
            return None
        return self._bootloader_recovery_selector(
            f"configured RCP serial device {self._settings.device} is absent", selected_target
        )

    def _bootloader_recovery_selector(self, reason: str, selected_target: bool) -> DfuSelector:
        """Authorize recovery only for one configured DFU USB identity."""

        if not (self._settings.allow_legacy_rcp or selected_target):
            raise UpdateError(
                f"{reason}. Bootloader recovery requires an exact "
                "manual firmware target or allow_legacy_rcp"
            )

        try:
            selector = self._flasher.bootloader_selector(
                self._settings.dfu_serial_number,
                self._settings.dfu_usb_path,
            )
            target = self._flasher.probe_dfu_target(selector)
        except DfuError as err:
            raise UpdateError(str(err)) from err
        if not target.present:
            detail = target.diagnostic or "no diagnostic output"
            raise UpdateError(
                f"{reason}; DFU target {DEFAULT_DFU_VID_PID!r} is not "
                f"present. "
                f"Last discovery output: {detail}"
            )
        if not target.ready:
            detail = target.diagnostic or "no diagnostic output"
            raise UpdateError(
                f"{reason}, but DFU target {DEFAULT_DFU_VID_PID!r} is present "
                "and nrfdfu could not communicate with it. "
                f"Last discovery output: {detail}"
            )
        return selector

    def _wait_for_expected_release(
        self,
        package: PreparedFirmware,
        dfu_selector: DfuSelector | None = None,
        progress: ProgressReporter | None = None,
    ) -> NcpVersion:
        # Secure DFU targets can take longer than the normal OTBR restart window to
        # switch USB identities and start the RCP application.
        timeout = max(self._settings.boot_timeout, _MIN_POST_DFU_BOOT_TIMEOUT)
        deadline = monotonic() + timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                version = self._spinel().get_ncp_version()
                self._verify_package(version, package)
                return version
            except (SpinelError, UpdateError) as err:
                last_error = err
                sleep(1)
        if dfu_selector is not None:
            try:
                target = self._flasher.probe_dfu_target(dfu_selector)
                if target.present:
                    assert target.target is not None
                    LOGGER.warning(
                        "RCP did not re-enumerate; requesting a final Secure DFU application reboot"
                    )
                    self._flasher.reboot_application(target.target)
                    self._settle_qemu_usb_reenumeration(progress, "RCP")
                    return self._wait_for_expected_release(package, progress=progress)
            except DfuError as err:
                raise UpdateError(f"RCP application reboot retry failed: {err}") from err
        raise UpdateError(f"RCP did not report the expected firmware after DFU: {last_error}")

    @staticmethod
    def _verify_package(version: NcpVersion, package: PreparedFirmware) -> None:
        RcpUpdater._verify_expected(
            version,
            package.hardware,
            package.ncs_version,
            package.zephyr_version,
        )

    @staticmethod
    def _verify_release(version: NcpVersion, release: FirmwareRelease) -> None:
        """Compatibility wrapper for callers of the former release-only helper."""

        RcpUpdater._verify_expected(
            version,
            release.hardware,
            release.ncs_version,
            release.zephyr_version,
        )

    @staticmethod
    def _verify_expected(
        version: NcpVersion, hardware: str, ncs_version: str, zephyr_version: str
    ) -> None:
        expected = {
            "hardware": hardware,
            "ncs_version": ncs_version,
            "zephyr_version": zephyr_version,
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
            raise UpdateError(
                "RCP version tags did not match the requested firmware: " + "; ".join(mismatches)
            )

    @staticmethod
    def _matches_package(version: NcpVersion, package: PreparedFirmware) -> bool:
        return (
            version.hardware == package.hardware
            and version.ncs_version == package.ncs_version
            and version.zephyr_version == package.zephyr_version
        )

    def _prepare_safe_update(self) -> bool:
        """Confirm OTBR is safely idle, or already offline for maintenance."""

        if not self._settings.safe_update:
            return False

        state = self._supervisor.addon_state(CORE_OTBR_ADDON_SLUG)
        if state in {"stopped", "not_running"}:
            return False
        if state not in {"started", "running"}:
            raise UpdateError(
                f"OTBR is in transitional state {state!r}; retry after it is started or stopped"
            )

        self._require_quiet_management_window()
        return True

    def _require_quiet_management_window(self) -> None:
        OtbrRestClient(CORE_OTBR_API_URL).require_quiet_management_window(
            self._settings.idle_window
        )

    def _require_otbr_healthy(self) -> None:
        client = OtbrRestClient(CORE_OTBR_API_URL)
        deadline = monotonic() + self._settings.boot_timeout
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                client.require_healthy()
                return
            except PreflightError as err:
                last_error = err
                sleep(1)
        raise UpdateError(f"OTBR did not become healthy after the RCP update: {last_error}")
