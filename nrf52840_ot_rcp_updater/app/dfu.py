"""nrfdfu-rs invocation with fixed target ownership and bounded diagnostics."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from .models import DEFAULT_DFU_VID_PID


class DfuError(RuntimeError):
    """The PCA10059 Secure DFU bootloader could not install the verified ELF."""


class DfuTransferError(DfuError):
    """nrfdfu reported an error after a transfer may have changed the target."""


_USB_SERIAL_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
NRFDFU_EXECUTABLE = "/usr/local/bin/nrfdfu"
_NRFDFU_ENVIRONMENT = {
    # The launcher contains Supervisor and MQTT credentials. nrfdfu needs no
    # inherited configuration, so give it a stable locale and executable path
    # instead of passing the service environment across this trust boundary.
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass(frozen=True)
class DfuTargetProbe:
    """The presence and nrfdfu readiness of one selected Secure DFU device."""

    present: bool
    diagnostic: str
    target: DfuTarget | None = None
    ready: bool = False


@dataclass(frozen=True)
class DfuTarget:
    """One exact Secure DFU device resolved from the Linux USB topology."""

    serial_number: str
    physical_path: Path
    serial_port: Path | None = None


@dataclass(frozen=True)
class DfuSelector:
    """Fail-closed identity for an expected Secure DFU target."""

    physical_path: Path | None
    serial_number: str | None
    usb_path: str | None = None


class NrfDfuFlasher:
    """Program one verified ELF through the stock PCA10059 Secure DFU bootloader."""

    def __init__(
        self,
        executable: str = NRFDFU_EXECUTABLE,
        ready_timeout: float = 30.0,
        sys_class_tty: Path = Path("/sys/class/tty"),
        sys_bus_usb: Path = Path("/sys/bus/usb/devices"),
        dev_directory: Path = Path("/dev"),
    ) -> None:
        self._executable = executable
        self._ready_timeout = ready_timeout
        self._sys_class_tty = sys_class_tty
        self._sys_bus_usb = sys_bus_usb
        self._dev_directory = dev_directory

    def selector_for_device(self, device: Path, configured_serial: str | None) -> DfuSelector:
        """Bind a future bootloader to the normal RCP's physical USB port."""

        if configured_serial is not None:
            self._validate_serial(configured_serial)
        return DfuSelector(
            physical_path=self._usb_parent_for_tty(device),
            serial_number=configured_serial,
        )

    def bootloader_selector(
        self, configured_serial: str | None, configured_usb_path: str | None
    ) -> DfuSelector:
        """Select a bootloader-only recovery target without assuming a tty exists."""

        if configured_serial is not None:
            self._validate_serial(configured_serial)
        if configured_usb_path is not None:
            self._validate_usb_path(configured_usb_path)
        return DfuSelector(
            physical_path=None,
            serial_number=configured_serial,
            usb_path=configured_usb_path,
        )

    def normal_usb_serial(self, device: Path) -> str | None:
        """Return the normal application's descriptor serial for diagnostics only."""

        parent = self._usb_parent_for_tty(device)
        return self._read_usb_serial(parent) if parent is not None else None

    def probe_dfu_target(self, selector: DfuSelector) -> DfuTargetProbe:
        """Probe one selected DFU device while retaining bounded diagnostics."""

        self._validate_selector(selector)
        self._require_executable()
        try:
            candidates = self._matching_dfu_targets(selector)
        except OSError as err:
            return DfuTargetProbe(
                False, f"cannot enumerate USB topology at {self._sys_bus_usb}: {err}"
            )
        if not candidates:
            location = ""
            if selector.physical_path is not None:
                location = " on the RCP's physical USB port"
            elif selector.usb_path is not None:
                location = f" on configured USB path {selector.usb_path}"
            return DfuTargetProbe(
                False,
                f"no USB target matching {DEFAULT_DFU_VID_PID}{location}",
            )
        if len(candidates) > 1:
            description = self._describe_targets(candidates)
            serials = {candidate.serial_number for candidate in candidates}
            if len(serials) == 1:
                return DfuTargetProbe(
                    False,
                    f"multiple USB targets with the same descriptor serial match "
                    f"{DEFAULT_DFU_VID_PID}: {description}; configure dfu_usb_path to one listed "
                    "topology path because dfu_serial_number cannot disambiguate them",
                )
            return DfuTargetProbe(
                False,
                f"multiple USB targets match {DEFAULT_DFU_VID_PID}: {description}; configure "
                "dfu_serial_number "
                "to disambiguate recovery",
            )
        target = candidates[0]
        try:
            ports = self._tty_ports_for_usb_device(target.physical_path)
        except OSError as err:
            return DfuTargetProbe(
                False, f"cannot enumerate serial topology at {self._sys_class_tty}: {err}"
            )
        if not ports:
            return DfuTargetProbe(
                True,
                f"DFU target {target.serial_number} has no serial endpoint under "
                f"{self._sys_class_tty}",
                target,
            )
        last_output = ""
        last_target: DfuTarget | None = None
        for port in ports:
            exact_target = DfuTarget(target.serial_number, target.physical_path, port)
            last_target = exact_target
            returncode, output = self._probe_dfu_target(exact_target)
            if returncode == 0:
                return DfuTargetProbe(True, output[-1024:], exact_target, ready=True)
            last_output = output
        return DfuTargetProbe(
            True,
            "nrfdfu could not communicate with the selected DFU target: "
            f"{last_output[-1024:] or 'no diagnostic output'}",
            last_target,
        )

    def flash(
        self,
        firmware: Path,
        selector: DfuSelector,
        application_version: int,
        hardware_version: int = 52,
    ) -> DfuTarget:
        if not firmware.is_file() or firmware.suffix != ".elf":
            raise DfuError("RCP firmware is missing or is not an ELF file")
        self._validate_selector(selector)
        if not 0 <= application_version <= 0xFFFFFFFF:
            raise DfuError("DFU application version must be an unsigned 32-bit integer")
        if not 0 <= hardware_version <= 0xFFFFFFFF:
            raise DfuError("DFU hardware version must be an unsigned 32-bit integer")
        self._require_executable()

        target = self._wait_for_dfu_target(selector)
        command = [
            self._executable,
            *self._target_arguments(target),
            "--fw-version",
            str(application_version),
            "--hw-version",
            str(hardware_version),
            # Secure DFU otherwise remains in its bootloader after a successful transfer.
            "--abort",
            str(firmware),
        ]
        returncode, output = self._run(command, timeout=300, action="nrfdfu DFU")
        if returncode != 0:
            raise DfuTransferError(
                f"nrfdfu failed ({returncode}): {output or 'no diagnostic output'}"
            )
        return target

    def reboot_application(self, target: DfuTarget) -> None:
        """Ask a verified Secure DFU target to leave its bootloader."""

        self._validate_serial(target.serial_number)
        self._require_executable()
        returncode, output = self._run(
            [self._executable, *self._target_arguments(target), "--abort"],
            timeout=30,
            action="nrfdfu application reboot",
        )
        if returncode != 0:
            raise DfuError(
                f"nrfdfu application reboot failed ({returncode}): "
                f"{output or 'no diagnostic output'}"
            )

    def _wait_for_dfu_target(self, selector: DfuSelector) -> DfuTarget:
        """Wait for the selected RCP to enumerate as its expected DFU USB identity."""

        deadline = monotonic() + self._ready_timeout
        last_output = ""
        target_seen = False
        while True:
            target = self.probe_dfu_target(selector)
            target_seen = target_seen or target.present
            if target.ready:
                assert target.target is not None
                return target.target
            last_output = target.diagnostic
            if monotonic() >= deadline:
                detail = last_output or "no diagnostic output"
                if target_seen:
                    raise DfuError(
                        f"PCA10059 DFU target {DEFAULT_DFU_VID_PID!r} appeared after the Spinel "
                        "bootloader reset, but nrfdfu could not communicate with it. "
                        f"Last discovery output: {detail}"
                    )
                raise DfuError(
                    f"PCA10059 DFU target {DEFAULT_DFU_VID_PID!r} did not appear after the Spinel "
                    "bootloader reset. nrfdfu requires the stock Nordic 1915:521f "
                    f"bootloader. Last discovery output: {detail}"
                )
            sleep(1)

    def _validate_serial(self, serial_number: str) -> None:
        if not _USB_SERIAL_RE.fullmatch(serial_number):
            raise DfuError("DFU USB serial contains unsupported characters")

    @staticmethod
    def _validate_usb_path(usb_path: str) -> None:
        if not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*", usb_path):
            raise DfuError("DFU USB path must look like a Linux USB path such as 2-3 or 2-3.1")

    def _validate_selector(self, selector: DfuSelector) -> None:
        if selector.serial_number is not None:
            self._validate_serial(selector.serial_number)
        if selector.usb_path is not None:
            self._validate_usb_path(selector.usb_path)
        if selector.physical_path is not None and selector.usb_path is not None:
            raise DfuError("DFU selector cannot combine a live physical path with dfu_usb_path")

    def _require_executable(self) -> None:
        if shutil.which(self._executable) is None:
            raise DfuError("nrfdfu is unavailable; the app image is incomplete")

    def _probe_dfu_target(self, target: DfuTarget) -> tuple[int, str]:
        return self._run(
            [self._executable, *self._target_arguments(target), "--get-images"],
            timeout=10,
            action="nrfdfu DFU discovery",
        )

    @staticmethod
    def _target_arguments(target: DfuTarget) -> list[str]:
        """Bind nrfdfu to the exact endpoint that passed the USB topology check."""

        if target.serial_port is None:
            raise DfuError("DFU target has no resolved serial endpoint")
        return ["--serial", target.serial_number, "--port", str(target.serial_port)]

    def _usb_parent_for_tty(self, device: Path) -> Path | None:
        """Return the physical USB device ancestor for a configured tty."""

        try:
            tty_name = device.resolve(strict=True).name
            node = (self._sys_class_tty / tty_name / "device").resolve(strict=True)
        except OSError:
            return None
        for parent in (node, *node.parents):
            if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
                return parent
        return None

    def _matching_dfu_targets(self, selector: DfuSelector) -> list[DfuTarget]:
        """Find a single bootloader by topology first, never by VID:PID alone when ambiguous."""

        candidates_by_path: dict[Path, DfuTarget] = {}
        paths = tuple(self._sys_bus_usb.iterdir())
        for candidate in paths:
            try:
                physical_path = candidate.resolve(strict=True)
                vid_pid = ":".join(
                    (
                        (physical_path / "idVendor").read_text(encoding="ascii").strip().lower(),
                        (physical_path / "idProduct").read_text(encoding="ascii").strip().lower(),
                    )
                )
            except OSError:
                continue
            if vid_pid != DEFAULT_DFU_VID_PID:
                continue
            if selector.physical_path is not None and physical_path != selector.physical_path:
                continue
            if selector.usb_path is not None and physical_path.name != selector.usb_path:
                continue
            serial_number = self._read_usb_serial(physical_path)
            if serial_number is None:
                continue
            if selector.serial_number is not None and serial_number != selector.serial_number:
                continue
            # /sys/bus/usb/devices can present aliases for one physical device.
            # The resolved device path is its stable identity for this preflight.
            candidates_by_path.setdefault(
                physical_path, DfuTarget(serial_number, physical_path)
            )
        return list(candidates_by_path.values())

    def _tty_ports_for_usb_device(self, physical_path: Path) -> list[Path]:
        """List USB serial endpoints that are proven to belong to one device path."""

        ports: list[Path] = []
        tty_paths = tuple(self._sys_class_tty.iterdir())
        for tty_path in tty_paths:
            try:
                node = (tty_path / "device").resolve(strict=True)
            except OSError:
                continue
            if any(parent == physical_path for parent in (node, *node.parents)):
                ports.append(self._dev_directory / tty_path.name)
        return sorted(ports)

    @staticmethod
    def _describe_targets(candidates: list[DfuTarget]) -> str:
        """Return a bounded, actionable description of ambiguous USB targets."""

        descriptions = [
            f"{target.serial_number} ({target.physical_path.name})" for target in candidates[:4]
        ]
        if len(candidates) > len(descriptions):
            descriptions.append(f"and {len(candidates) - len(descriptions)} more")
        return ", ".join(descriptions)

    @staticmethod
    def _read_usb_serial(physical_path: Path) -> str | None:
        try:
            serial = (physical_path / "serial").read_text(encoding="ascii").strip()
        except OSError:
            return None
        return serial if _USB_SERIAL_RE.fullmatch(serial) else None

    def _run(self, command: list[str], timeout: float, action: str) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=_NRFDFU_ENVIRONMENT,
                close_fds=True,
            )
        except OSError as err:
            raise DfuError(f"unable to execute {action}: {err}") from err
        except subprocess.TimeoutExpired as err:
            raise DfuError(f"{action} exceeded its {int(timeout)} second timeout") from err
        output = completed.stdout or b""
        if isinstance(output, bytes):
            text = output[-8192:].decode("utf-8", "replace").strip()
        else:
            text = output[-8192:].strip()
        return completed.returncode, text
