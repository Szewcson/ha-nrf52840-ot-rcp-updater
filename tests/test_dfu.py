from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.dfu import (
    NRFDFU_EXECUTABLE,
    DfuSelector,
    DfuTarget,
    DfuTargetProbe,
    DfuTransferError,
    NrfDfuFlasher,
)


def _selector(
    physical_path: Path | None = None,
    serial_number: str | None = "CC2180B1200E",
    usb_path: str | None = None,
) -> DfuSelector:
    return DfuSelector(physical_path, serial_number, usb_path)


def _target(physical_path: Path = Path("/sys/devices/1-2")) -> DfuTarget:
    return DfuTarget("CC2180B1200E", physical_path, Path("/dev/ttyACM0"))


def _usb_device(path: Path, serial_number: str) -> None:
    path.mkdir(parents=True)
    (path / "idVendor").write_text("1915\n", encoding="ascii")
    (path / "idProduct").write_text("521f\n", encoding="ascii")
    (path / "serial").write_text(f"{serial_number}\n", encoding="ascii")


class NrfDfuFlasherTests(unittest.TestCase):
    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_flashes_the_exact_discovered_target_and_passes_versions(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"firmware complete\n")
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            firmware.touch()
            flasher = NrfDfuFlasher()
            with patch.object(flasher, "_wait_for_dfu_target", return_value=_target()):
                self.assertEqual(flasher.flash(firmware, _selector(), 3_004_000), _target())

        self.assertEqual(
            run.call_args.args[0],
            [
                NRFDFU_EXECUTABLE,
                "--serial",
                "CC2180B1200E",
                "--port",
                "/dev/ttyACM0",
                "--fw-version",
                "3004000",
                "--hw-version",
                "52",
                "--abort",
                str(firmware),
            ],
        )

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    def test_reports_dfu_reenumeration_timeout(self, which) -> None:
        del which
        flasher = NrfDfuFlasher(ready_timeout=0)
        with patch.object(flasher, "probe_dfu_target") as probe:
            probe.return_value = DfuTargetProbe(False, "no USB target matching 1915:521f")
            with self.assertRaisesRegex(RuntimeError, "no USB target matching 1915:521f"):
                flasher._wait_for_dfu_target(_selector())

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_retains_diagnostics_after_probing_the_selected_target(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"* image 0: bootloader\n")
        flasher = NrfDfuFlasher()
        with (
            patch.object(flasher, "_matching_dfu_targets", return_value=[_target()]),
            patch.object(flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM0")]),
        ):
            target = flasher.probe_dfu_target(_selector())

        self.assertTrue(target.present)
        self.assertTrue(target.ready)
        self.assertEqual(target.target, _target())
        self.assertEqual(target.diagnostic, "* image 0: bootloader")
        self.assertEqual(
            run.call_args.args[0],
            [
                NRFDFU_EXECUTABLE,
                "--serial",
                "CC2180B1200E",
                "--port",
                "/dev/ttyACM0",
                "--get-images",
            ],
        )

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_reports_a_transfer_failure_after_a_target_was_selected(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 1, stdout=b"invalid firmware version\n")
        with TemporaryDirectory() as directory:
            firmware = Path(directory) / "rcp.elf"
            firmware.touch()
            flasher = NrfDfuFlasher()
            with (
                patch.object(flasher, "_wait_for_dfu_target", return_value=_target()),
                self.assertRaisesRegex(DfuTransferError, "invalid firmware version"),
            ):
                flasher.flash(firmware, _selector(), 3_004_000)

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_reboots_a_verified_dfu_target_into_its_application(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"application rebooted\n")

        NrfDfuFlasher().reboot_application(_target())

        self.assertEqual(
            run.call_args.args[0],
            [NRFDFU_EXECUTABLE, "--serial", "CC2180B1200E", "--port", "/dev/ttyACM0", "--abort"],
        )

    def test_records_the_normal_rcp_physical_usb_path_not_its_different_serial(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "dev" / "ttyACM0"
            device.parent.mkdir()
            device.touch()
            normal_rcp = root / "sys" / "devices" / "1-2"
            _usb_device(normal_rcp, "5DECD9DA9760A913")
            sys_class_tty = root / "sys" / "class" / "tty"
            tty_device = sys_class_tty / "ttyACM0" / "device"
            tty_device.parent.mkdir(parents=True)
            tty_device.symlink_to(normal_rcp)

            selector = NrfDfuFlasher(sys_class_tty=sys_class_tty).selector_for_device(
                device, None
            )

        self.assertEqual(selector.physical_path, normal_rcp)
        self.assertIsNone(selector.serial_number)

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_accepts_only_the_bootloader_on_the_recorded_physical_usb_path(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"ready\n")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            same_port = root / "sys" / "devices" / "1-2"
            other_port = root / "sys" / "devices" / "1-3"
            _usb_device(same_port, "CC2180B1200E")
            _usb_device(other_port, "OTHERDFU")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "1-2").symlink_to(same_port)
            (sys_bus_usb / "1-3").symlink_to(other_port)

            flasher = NrfDfuFlasher(sys_bus_usb=sys_bus_usb)
            with patch.object(
                flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM0")]
            ):
                target = flasher.probe_dfu_target(_selector(same_port))

        self.assertTrue(target.present)
        self.assertTrue(target.ready)
        self.assertEqual(target.target, DfuTarget("CC2180B1200E", same_port, Path("/dev/ttyACM0")))
        self.assertEqual(run.call_args.args[0][2], "CC2180B1200E")
        self.assertEqual(run.call_args.args[0][4], "/dev/ttyACM0")

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    def test_bootloader_only_recovery_rejects_multiple_vid_pid_candidates(self, which) -> None:
        del which
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            first = root / "sys" / "devices" / "1-2"
            second = root / "sys" / "devices" / "1-3"
            _usb_device(first, "FIRST")
            _usb_device(second, "SECOND")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "1-2").symlink_to(first)
            (sys_bus_usb / "1-3").symlink_to(second)

            target = NrfDfuFlasher(sys_bus_usb=sys_bus_usb).probe_dfu_target(
                _selector(serial_number=None)
            )

        self.assertFalse(target.present)
        self.assertIn("multiple USB targets", target.diagnostic)
        self.assertIn("FIRST (1-2)", target.diagnostic)
        self.assertIn("SECOND (1-3)", target.diagnostic)

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    def test_bootloader_only_recovery_explains_when_duplicate_serials_need_usb_path(
        self, which
    ) -> None:
        del which
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            first = root / "sys" / "devices" / "2-3"
            second = root / "sys" / "devices" / "2-5"
            _usb_device(first, "CC2180B1200E")
            _usb_device(second, "CC2180B1200E")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "2-3").symlink_to(first)
            (sys_bus_usb / "2-5").symlink_to(second)

            target = NrfDfuFlasher(sys_bus_usb=sys_bus_usb).probe_dfu_target(
                _selector(serial_number="CC2180B1200E")
            )

        self.assertFalse(target.present)
        self.assertIn("same descriptor serial", target.diagnostic)
        self.assertIn("dfu_usb_path", target.diagnostic)

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_bootloader_only_recovery_uses_configured_usb_path_for_duplicate_serials(
        self, run, which
    ) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"ready\n")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            first = root / "sys" / "devices" / "2-3"
            second = root / "sys" / "devices" / "2-5"
            _usb_device(first, "CC2180B1200E")
            _usb_device(second, "CC2180B1200E")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "2-3").symlink_to(first)
            (sys_bus_usb / "2-5").symlink_to(second)

            flasher = NrfDfuFlasher(sys_bus_usb=sys_bus_usb)
            selector = flasher.bootloader_selector("CC2180B1200E", "2-5")
            with patch.object(
                flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM1")]
            ):
                target = flasher.probe_dfu_target(selector)

        self.assertTrue(target.present)
        self.assertTrue(target.ready)
        self.assertEqual(target.target, DfuTarget("CC2180B1200E", second, Path("/dev/ttyACM1")))

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    def test_reports_an_inaccessible_usb_topology(self, which) -> None:
        del which
        flasher = NrfDfuFlasher(sys_bus_usb=Path("/missing/usb/devices"))

        probe = flasher.probe_dfu_target(_selector(serial_number=None))

        self.assertFalse(probe.present)
        self.assertIn("cannot enumerate USB topology", probe.diagnostic)

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_bootloader_only_recovery_deduplicates_usb_sysfs_aliases(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"ready\n")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            device = root / "sys" / "devices" / "1-2"
            _usb_device(device, "CC2180B1200E")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "1-2").symlink_to(device)
            (sys_bus_usb / "alias-for-1-2").symlink_to(device)

            flasher = NrfDfuFlasher(sys_bus_usb=sys_bus_usb)
            with patch.object(
                flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM0")]
            ):
                target = flasher.probe_dfu_target(_selector(serial_number=None))

        self.assertTrue(target.present)
        self.assertTrue(target.ready)
        self.assertEqual(target.target, DfuTarget("CC2180B1200E", device, Path("/dev/ttyACM0")))
        self.assertEqual(run.call_args.args[0][2], "CC2180B1200E")
        self.assertEqual(run.call_args.args[0][4], "/dev/ttyACM0")

    @patch("app.dfu.shutil.which", return_value="/usr/local/bin/nrfdfu")
    @patch("app.dfu.subprocess.run")
    def test_optional_dfu_serial_disambiguates_bootloader_only_recovery(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"ready\n")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sys_bus_usb = root / "sys" / "bus" / "usb" / "devices"
            first = root / "sys" / "devices" / "1-2"
            second = root / "sys" / "devices" / "1-3"
            _usb_device(first, "FIRST")
            _usb_device(second, "SECOND")
            sys_bus_usb.mkdir(parents=True)
            (sys_bus_usb / "1-2").symlink_to(first)
            (sys_bus_usb / "1-3").symlink_to(second)

            flasher = NrfDfuFlasher(sys_bus_usb=sys_bus_usb)
            with patch.object(
                flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM1")]
            ):
                target = flasher.probe_dfu_target(_selector(serial_number="SECOND"))

        self.assertTrue(target.present)
        self.assertTrue(target.ready)
        self.assertEqual(target.target, DfuTarget("SECOND", second, Path("/dev/ttyACM1")))
        self.assertEqual(run.call_args.args[0][2], "SECOND")
        self.assertEqual(run.call_args.args[0][4], "/dev/ttyACM1")

    def test_resolves_only_serial_endpoints_owned_by_the_selected_usb_device(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "sys" / "devices" / "1-2"
            other = root / "sys" / "devices" / "1-3"
            _usb_device(selected, "CC2180B1200E")
            _usb_device(other, "OTHERDFU")
            sys_class_tty = root / "sys" / "class" / "tty"
            selected_tty = sys_class_tty / "ttyACM0" / "device"
            other_tty = sys_class_tty / "ttyACM1" / "device"
            selected_tty.parent.mkdir(parents=True)
            other_tty.parent.mkdir(parents=True)
            selected_tty.symlink_to(selected)
            other_tty.symlink_to(other)

            ports = NrfDfuFlasher(
                sys_class_tty=sys_class_tty, dev_directory=root / "dev"
            )._tty_ports_for_usb_device(selected)

        self.assertEqual(ports, [root / "dev" / "ttyACM0"])

    @patch("app.dfu.shutil.which", return_value=NRFDFU_EXECUTABLE)
    @patch("app.dfu.subprocess.run")
    def test_reports_a_present_target_when_nrfdfu_cannot_probe_it(self, run, which) -> None:
        del which
        run.return_value = subprocess.CompletedProcess([], 1, stdout=b"permission denied\n")
        flasher = NrfDfuFlasher()
        with (
            patch.object(flasher, "_matching_dfu_targets", return_value=[_target()]),
            patch.object(flasher, "_tty_ports_for_usb_device", return_value=[Path("/dev/ttyACM0")]),
        ):
            probe = flasher.probe_dfu_target(_selector())

        self.assertTrue(probe.present)
        self.assertFalse(probe.ready)
        self.assertEqual(probe.target, _target())
        self.assertIn("nrfdfu could not communicate", probe.diagnostic)
        self.assertIn("permission denied", probe.diagnostic)


if __name__ == "__main__":
    unittest.main()
