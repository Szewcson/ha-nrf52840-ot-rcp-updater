from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.dfu import DfuSelector, DfuTarget, DfuTargetProbe, DfuTransferError
from app.models import Artifact, FirmwareRelease, NcpVersion, Settings
from app.spinel import SpinelError
from app.state import StateError, StateStore, update_lock
from app.updater import RcpUpdater, RescanDeferred, UpdateError


def _settings(
    dfu_serial_number: str | None = None,
    allow_legacy_rcp: bool = True,
    dfu_usb_path: str | None = None,
    device: str = "/dev/null",
    qemu_usb_reenumeration_workaround: bool = False,
    safe_update: bool = True,
) -> Settings:
    return Settings.from_mapping(
        {
            "device": device,
            "baudrate": 460800,
            "safe_update": safe_update,
            "qemu_usb_reenumeration_workaround": qemu_usb_reenumeration_workaround,
            "allow_legacy_rcp": allow_legacy_rcp,
            "allow_prereleases": False,
            "dfu_serial_number": dfu_serial_number,
            "dfu_usb_path": dfu_usb_path,
            "manifest_poll_interval": 3600,
            "idle_window": 20,
            "boot_timeout": 45,
        }
    )


def _release(version: str, zephyr_version: str = "4.4.0") -> FirmwareRelease:
    return FirmwareRelease(
        hardware="PCA10059",
        ncs_version=version,
        zephyr_version=zephyr_version,
        dfu_application_version=3_004_000,
        artifact=Artifact(
            url="https://example.invalid/rcp.elf",
            sha256="0" * 64,
            filename="rcp.elf",
            signature_url="https://example.invalid/rcp.elf.sig",
        ),
        release_url="https://example.invalid/release",
        release_summary="Test release",
    )


class _Supervisor:
    def __init__(self, state: str, context_was_running: bool | None = None) -> None:
        self._state = state
        self._context_was_running = context_was_running
        self.temporarily_stop_calls = 0

    def addon_state(self, addon_slug: str) -> str:
        return self._state

    @contextmanager
    def temporarily_stop(self, addon_slug: str):
        self.temporarily_stop_calls += 1
        if self._context_was_running is None:
            yield self._state in {"started", "running"}
        else:
            yield self._context_was_running


class _Spinel:
    def __init__(self, *versions: NcpVersion) -> None:
        self._versions = list(versions)
        self.reset_requested = False

    def get_ncp_version(self) -> NcpVersion:
        if len(self._versions) > 1:
            return self._versions.pop(0)
        return self._versions[0]

    def reset_bootloader(self) -> None:
        self.reset_requested = True


class _Flasher:
    def __init__(
        self,
        dfu_ready: bool = True,
        dfu_present: bool | None = None,
        flash_error: DfuTransferError | None = None,
    ) -> None:
        self._dfu_ready = dfu_ready
        self._dfu_present = dfu_ready if dfu_present is None else dfu_present
        self._flash_error = flash_error
        self.dfu_ready_requests: list[DfuSelector] = []
        self.selector_requests: list[DfuSelector] = []
        self.flash_requests: list[tuple[Path, DfuSelector, int]] = []
        self.reboot_requests: list[DfuTarget] = []

    def selector_for_device(self, device: Path, configured_serial: str | None) -> DfuSelector:
        del device
        selector = DfuSelector(Path("/sys/devices/mock-rcp"), configured_serial)
        self.selector_requests.append(selector)
        return selector

    def bootloader_selector(
        self, configured_serial: str | None, configured_usb_path: str | None
    ) -> DfuSelector:
        selector = DfuSelector(None, configured_serial, configured_usb_path)
        self.selector_requests.append(selector)
        return selector

    def normal_usb_serial(self, device: Path) -> str:
        del device
        return "5DECD9DA9760A913"

    def probe_dfu_target(self, selector: DfuSelector) -> DfuTargetProbe:
        self.dfu_ready_requests.append(selector)
        target = DfuTarget(
            "CC2180B1200E", Path("/sys/devices/mock-rcp"), Path("/dev/ttyACM0")
        )
        return DfuTargetProbe(
            present=self._dfu_present,
            diagnostic=(
                "DFU target found"
                if self._dfu_ready
                else "DFU target is present but nrfdfu cannot access it"
                if self._dfu_present
                else "no matching USB serial device"
            ),
            target=target if self._dfu_present else None,
            ready=self._dfu_ready,
        )

    def flash(self, firmware: Path, selector: DfuSelector, application_version: int) -> DfuTarget:
        self.flash_requests.append((firmware, selector, application_version))
        if self._flash_error is not None:
            raise self._flash_error
        return DfuTarget(
            "CC2180B1200E", Path("/sys/devices/mock-rcp"), Path("/dev/ttyACM0")
        )

    def reboot_application(self, target: DfuTarget) -> None:
        self.reboot_requests.append(target)


class _BootloaderSpinel:
    def __init__(self, expected: NcpVersion) -> None:
        self._expected = expected
        self._version_reads = 0
        self.reset_requested = False

    def get_ncp_version(self) -> NcpVersion:
        self._version_reads += 1
        if self._version_reads == 1:
            raise SpinelError("no Spinel response; RCP is in Secure DFU mode")
        return self._expected

    def reset_bootloader(self) -> None:
        self.reset_requested = True


class _DelayedBootSpinel:
    def __init__(self, expected: NcpVersion, failed_reads: int) -> None:
        self._expected = expected
        self._failed_reads = failed_reads
        self.version_reads = 0

    def get_ncp_version(self) -> NcpVersion:
        self.version_reads += 1
        if self.version_reads <= self._failed_reads:
            raise SpinelError("RCP USB re-enumeration is still in progress")
        return self._expected


class ManualTargetTests(unittest.TestCase):
    def test_tagged_rcp_downgrade_requires_an_explicit_manual_target(self) -> None:
        current = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory))
            updater = RcpUpdater(_settings(), state_store, supervisor=object(), flasher=object())
            with self.assertRaisesRegex(UpdateError, "manual firmware target"):
                updater._validate_current_ncp(current, _release("3.3.4"))

            approved = RcpUpdater(
                _settings(), state_store, supervisor=object(), flasher=object()
            )
            approved._validate_current_ncp(current, _release("3.3.4"), selected_target=True)


class SafeUpdateFallbackTests(unittest.TestCase):
    def test_current_version_defers_while_otbr_is_transitional(self) -> None:
        class TransitionalSupervisor(_Supervisor):
            def addon_state(self, addon_slug: str) -> str:
                del addon_slug
                return "starting"

        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(),
                StateStore(Path(directory)),
                supervisor=TransitionalSupervisor("starting"),
                flasher=_Flasher(),
            )

            with self.assertRaisesRegex(RescanDeferred, "not ready"):
                updater.current_version()

    def test_reports_the_dfu_discovery_output_in_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E"),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=_Flasher(dfu_ready=False),
            )

            diagnostics = updater.diagnostics()

        self.assertFalse(diagnostics["dfu_target_ready"])
        self.assertEqual(diagnostics["dfu_probe_output"], "no matching USB serial device")

    def test_allows_an_already_stopped_otbr_without_rest_api_access(self) -> None:
        current = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=object(),
            )
            updater._spinel = lambda: _Spinel(current)

            with patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"):
                self.assertEqual(updater.install(_release("3.4.0")), current)

    def test_rejects_a_missing_rcp_and_dfu_before_stopping_otbr(self) -> None:
        supervisor = _Supervisor("running")
        flasher = _Flasher(dfu_ready=False)
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(
                    dfu_serial_number="CC2180B1200E",
                    device="/dev/serial/by-id/missing-nrf52840",
                ),
                StateStore(Path(directory)),
                supervisor=supervisor,
                flasher=flasher,
            )

            with (
                patch("app.updater.download_artifact") as download,
                self.assertRaisesRegex(
                    UpdateError,
                    "configured RCP serial device /dev/serial/by-id/missing-nrf52840 is absent; "
                    "DFU target '1915:521f' is not present",
                ),
            ):
                updater.install(_release("3.4.0"))

        download.assert_not_called()
        self.assertEqual(supervisor.temporarily_stop_calls, 0)
        self.assertEqual(flasher.flash_requests, [])

    def test_rejects_if_otbr_state_changes_between_preflight_and_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped", context_was_running=True),
                flasher=object(),
            )

            with (
                patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"),
                self.assertRaisesRegex(UpdateError, "state changed"),
            ):
                updater.install(_release("3.4.0"))


class NrfDfuUpdateTests(unittest.TestCase):
    def test_keeps_the_transaction_lock_through_durable_verified_state(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory))
            updater = RcpUpdater(
                _settings(safe_update=False),
                state_store,
                supervisor=_Supervisor("stopped"),
                flasher=object(),
            )
            updater._spinel = lambda: _Spinel(expected)
            lock_outcomes: list[str] = []

            def progress(percentage: int, stage: str) -> None:
                del stage
                if percentage == 100:
                    try:
                        with update_lock(Path(directory)):
                            lock_outcomes.append("acquired")
                    except StateError:
                        lock_outcomes.append("blocked")

            with patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"):
                self.assertEqual(updater.install(_release("3.4.0"), progress=progress), expected)

            self.assertEqual(lock_outcomes, ["blocked"])
            with update_lock(Path(directory)):
                pass

    def test_reconciles_a_verified_firmware_after_nrfdfu_reports_a_transfer_error(self) -> None:
        before = NcpVersion(
            raw="HW/PCA10059 NCS/3.5.0-preview1 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.5.0-preview1",
            zephyr_version="4.4.0",
        )
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.3.4 ZEPHYR/4.3.99",
            hardware="PCA10059",
            ncs_version="3.3.4",
            zephyr_version="4.3.99",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            flasher = _Flasher(flash_error=DfuTransferError("nrfdfu transfer interrupted"))
            state_store = StateStore(Path(directory))
            updater = RcpUpdater(
                _settings(),
                state_store,
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            spinel = _Spinel(before, expected)
            updater._spinel = lambda: spinel

            with patch("app.updater.download_artifact", return_value=firmware):
                self.assertEqual(
                    updater.install(_release("3.3.4", "4.3.99"), selected_target=True), expected
                )
            self.assertEqual(state_store.load()["installed"]["ncs_version"], "3.3.4")

        self.assertEqual(
            flasher.flash_requests,
            [
                (
                    firmware,
                    DfuSelector(Path("/sys/devices/mock-rcp"), None),
                    3_004_000,
                )
            ],
        )

    def test_retries_the_application_reboot_when_dfu_remains_present(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        flasher = _Flasher()
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(),
                StateStore(Path(directory)),
                supervisor=object(),
                flasher=flasher,
            )
            updater._spinel = lambda: _Spinel(expected)

            # The first window expires immediately; the reboot retry gets a fresh window.
            with patch("app.updater.monotonic", side_effect=[0, 90, 0, 0]):
                self.assertEqual(
                    updater._wait_for_expected_release(
                        _release("3.4.0"), DfuSelector(None, "CC2180B1200E")
                    ),
                    expected,
                )

        self.assertEqual(
            flasher.reboot_requests,
            [DfuTarget("CC2180B1200E", Path("/sys/devices/mock-rcp"), Path("/dev/ttyACM0"))],
        )

    def test_post_dfu_verification_waits_past_a_short_configured_timeout(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        spinel = _DelayedBootSpinel(expected, failed_reads=46)
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(),
                StateStore(Path(directory)),
                supervisor=object(),
                flasher=object(),
            )
            updater._spinel = lambda: spinel

            # The first value sets the deadline; later values advance one second per probe.
            with (
                patch("app.updater.monotonic", side_effect=[0, *range(47)]),
                patch("app.updater.sleep"),
            ):
                self.assertEqual(updater._wait_for_expected_release(_release("3.4.0")), expected)

        self.assertEqual(spinel.version_reads, 47)

    def test_flashes_the_manifest_elf_and_verifies_the_result(self) -> None:
        before = NcpVersion(
            raw="HW/PCA10059 NCS/3.3.4 ZEPHYR/4.3.99",
            hardware="PCA10059",
            ncs_version="3.3.4",
            zephyr_version="4.3.99",
        )
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            flasher = _Flasher()
            spinel = _Spinel(before, expected)
            state_store = StateStore(Path(directory))
            updater = RcpUpdater(
                _settings(),
                state_store,
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            updater._spinel = lambda: spinel
            progress: list[tuple[int, str]] = []

            with patch("app.updater.download_artifact", return_value=firmware):
                self.assertEqual(
                    updater.install(
                        _release("3.4.0"),
                        progress=lambda percentage, stage: progress.append((percentage, stage)),
                    ),
                    expected,
                )
            self.assertNotIn("dfu_activity_at", state_store.load())

        self.assertTrue(spinel.reset_requested)
        self.assertEqual(
            flasher.flash_requests,
            [
                (
                    firmware,
                    DfuSelector(Path("/sys/devices/mock-rcp"), None),
                    3_004_000,
                )
            ],
        )
        self.assertEqual(
            progress,
            [
                (5, "Validating release"),
                (15, "Downloading firmware"),
                (35, "Preparing OTBR and RCP"),
                (55, "Entering Secure DFU"),
                (70, "Flashing firmware"),
                (85, "Verifying RCP firmware"),
                (100, "Update verified"),
            ],
        )

    def test_qemu_workaround_settles_before_each_first_probe(self) -> None:
        before = NcpVersion(
            raw="HW/PCA10059 NCS/3.3.4 ZEPHYR/4.3.99",
            hardware="PCA10059",
            ncs_version="3.3.4",
            zephyr_version="4.3.99",
        )
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            spinel = _Spinel(before, expected)
            updater = RcpUpdater(
                _settings(qemu_usb_reenumeration_workaround=True),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=_Flasher(),
            )
            updater._spinel = lambda: spinel
            progress: list[tuple[int, str]] = []

            with (
                patch("app.updater.download_artifact", return_value=firmware),
                patch("app.updater.sleep") as sleep,
            ):
                self.assertEqual(
                    updater.install(
                        _release("3.4.0"),
                        progress=lambda percentage, stage: progress.append((percentage, stage)),
                    ),
                    expected,
                )

        self.assertEqual(sleep.call_args_list, [call(8), call(8)])
        self.assertIn((60, "Waiting for Secure DFU USB re-enumeration"), progress)
        self.assertIn((85, "Waiting for RCP USB re-enumeration"), progress)

    def test_qemu_workaround_settles_bootloader_recovery_before_spinel(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            flasher = _Flasher()
            spinel = _BootloaderSpinel(expected)
            updater = RcpUpdater(
                _settings(
                    dfu_serial_number="CC2180B1200E",
                    qemu_usb_reenumeration_workaround=True,
                ),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            updater._spinel = lambda: spinel

            with (
                patch("app.updater.download_artifact", return_value=firmware),
                patch("app.updater.sleep") as sleep,
            ):
                self.assertEqual(updater.install(_release("3.4.0")), expected)

        self.assertFalse(spinel.reset_requested)
        self.assertEqual(sleep.call_args_list, [call(8)])

    def test_recovers_an_already_bootloader_only_rcp_without_resetting_it(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            flasher = _Flasher()
            spinel = _BootloaderSpinel(expected)
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E", dfu_usb_path="2-3"),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            updater._spinel = lambda: spinel

            with patch("app.updater.download_artifact", return_value=firmware):
                self.assertEqual(updater.install(_release("3.4.0")), expected)

        self.assertFalse(spinel.reset_requested)
        self.assertEqual(
            flasher.dfu_ready_requests,
            [DfuSelector(None, "CC2180B1200E", "2-3")],
        )
        self.assertEqual(
            flasher.selector_requests,
            [DfuSelector(None, "CC2180B1200E", "2-3")],
        )
        self.assertEqual(
            flasher.flash_requests,
            [
                (
                    firmware,
                    DfuSelector(None, "CC2180B1200E", "2-3"),
                    3_004_000,
                )
            ],
        )

    def test_selected_target_recovers_an_already_bootloader_only_rcp(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            flasher = _Flasher()
            spinel = _BootloaderSpinel(expected)
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E"),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            updater._spinel = lambda: spinel

            with patch("app.updater.download_artifact", return_value=firmware):
                self.assertEqual(updater.install(_release("3.4.0"), selected_target=True), expected)

        self.assertEqual(
            flasher.dfu_ready_requests,
            [DfuSelector(None, "CC2180B1200E")],
        )

    def test_selected_target_recovers_without_legacy_opt_in(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            flasher = _Flasher()
            spinel = _BootloaderSpinel(expected)
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E", allow_legacy_rcp=False),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=flasher,
            )
            updater._spinel = lambda: spinel

            with patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"):
                self.assertEqual(updater.install(_release("3.4.0"), selected_target=True), expected)

        self.assertEqual(
            flasher.dfu_ready_requests,
            [DfuSelector(None, "CC2180B1200E")],
        )

    def test_rejects_bootloader_recovery_without_manual_target_or_legacy_opt_in(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E", allow_legacy_rcp=False),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=_Flasher(),
            )
            updater._spinel = lambda: _BootloaderSpinel(expected)

            with (
                patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"),
                self.assertRaisesRegex(UpdateError, "manual firmware target or allow_legacy_rcp"),
            ):
                updater.install(_release("3.4.0"))

    def test_rejects_bootloader_recovery_when_the_exact_dfu_target_is_absent(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E"),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=_Flasher(dfu_ready=False),
            )
            updater._spinel = lambda: _BootloaderSpinel(expected)

            with (
                patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"),
                self.assertRaisesRegex(
                    UpdateError, "Last discovery output: no matching USB serial device"
                ),
            ):
                updater.install(_release("3.4.0"))

    def test_reports_a_present_but_inaccessible_dfu_target(self) -> None:
        expected = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        with TemporaryDirectory() as directory:
            updater = RcpUpdater(
                _settings(dfu_serial_number="CC2180B1200E"),
                StateStore(Path(directory)),
                supervisor=_Supervisor("stopped"),
                flasher=_Flasher(dfu_ready=False, dfu_present=True),
            )
            updater._spinel = lambda: _BootloaderSpinel(expected)

            with (
                patch("app.updater.download_artifact", return_value=Path(directory) / "rcp.elf"),
                self.assertRaisesRegex(UpdateError, "DFU target '1915:521f' is present"),
            ):
                updater.install(_release("3.4.0"))

    def test_rejects_a_post_flash_version_tag_mismatch(self) -> None:
        release = _release("3.4.0")
        reported = NcpVersion(
            raw="HW/PCA10059 NCS/3.4.0 ZEPHYR/4.3.99",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.3.99",
        )

        with self.assertRaisesRegex(UpdateError, "zephyr_version"):
            RcpUpdater._verify_release(reported, release)


if __name__ == "__main__":
    unittest.main()
