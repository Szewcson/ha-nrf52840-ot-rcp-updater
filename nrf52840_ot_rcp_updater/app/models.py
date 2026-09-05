"""Validated data exchanged by the updater components."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class ValidationError(ValueError):
    """Raised when external configuration or release metadata is unsafe."""


_VERSION_RE = re.compile(
    r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"(?:-(?P<prerelease>preview|rc)(?P<sequence>[1-9][0-9]*))?$"
)
_MINOR_LINE_RE = re.compile(r"^[0-9]+\.[0-9]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_FILENAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,160}\.elf$")
_USB_TOPOLOGY_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*$")

DEFAULT_BAUDRATE = 1_000_000
SUPPORTED_HARDWARE = "PCA10059"
CORE_OTBR_ADDON_SLUG = "core_openthread_border_router"
CORE_OTBR_API_URL = "http://127.0.0.1:8081"
DEFAULT_SAFE_UPDATE = True
DEFAULT_ALLOW_LEGACY_RCP = False
DEFAULT_ALLOW_PRERELEASES = False
DEFAULT_QEMU_USB_REENUMERATION_WORKAROUND = False
DEFAULT_DFU_VID_PID = "1915:521f"
FIRMWARE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Szewcson/ha-nrf52840-ot-rcp-updater/"
    "firmware/manifest.json"
)
DEFAULT_MANIFEST_POLL_INTERVAL = 3600
DEFAULT_IDLE_WINDOW = 20
DEFAULT_BOOT_TIMEOUT = 90


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def validate_version(value: object, name: str) -> str:
    version = _require_string(value, name)
    if not _VERSION_RE.fullmatch(version):
        raise ValidationError(f"{name} has an invalid version format: {version!r}")
    return version


def version_key(value: str) -> tuple[int, int, int, int, int]:
    """Order the Nordic preview, release-candidate, and final tag forms."""

    version = validate_version(value, "version")
    match = _VERSION_RE.fullmatch(version)
    assert match is not None
    prerelease = match.group("prerelease")
    if prerelease is None:
        stage, sequence = 2, 0
    else:
        stage = 0 if prerelease == "preview" else 1
        sequence = int(match.group("sequence") or "0")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        stage,
        sequence,
    )


def is_prerelease(value: str) -> bool:
    """Return whether a validated NCS version is a Nordic preview or RC."""

    version = validate_version(value, "version")
    match = _VERSION_RE.fullmatch(version)
    assert match is not None
    return match.group("prerelease") is not None


def minor_line(value: str) -> str:
    """Return a normalized major.minor line from a validated NCS version."""

    version = validate_version(value, "version")
    match = _VERSION_RE.fullmatch(version)
    assert match is not None
    return f"{int(match.group('major'))}.{int(match.group('minor'))}"


def validate_minor_line(value: object, name: str) -> str:
    line = _require_string(value, name)
    if not _MINOR_LINE_RE.fullmatch(line):
        raise ValidationError(f"{name} must have MAJOR.MINOR form")
    major, minor = line.split(".")
    return f"{int(major)}.{int(minor)}"


def validate_token(value: object, name: str) -> str:
    token = _require_string(value, name)
    if not _TOKEN_RE.fullmatch(token):
        raise ValidationError(f"{name} contains unsupported characters: {token!r}")
    return token


def validate_usb_topology(value: object, name: str) -> str:
    """Validate a Linux USB device topology name, never a filesystem path."""

    topology = _require_string(value, name)
    if not _USB_TOPOLOGY_RE.fullmatch(topology):
        raise ValidationError(f"{name} must look like a Linux USB path such as 2-3 or 2-3.1")
    return topology


def validate_dfu_application_version(value: object, name: str) -> int:
    """Validate the unsigned version used by the stock Secure DFU bootloader."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValidationError(f"{name} must be an unsigned 32-bit integer")
    return value


def validate_artifact_filename(value: object, name: str) -> str:
    """Accept one portable firmware basename, never a filesystem path."""

    filename = _require_string(value, name)
    if not _ARTIFACT_FILENAME_RE.fullmatch(filename):
        raise ValidationError(f"{name} must be a simple .elf filename")
    return filename


@dataclass(frozen=True)
class Artifact:
    """An immutable RCP ELF selected from a release manifest."""

    url: str
    sha256: str
    filename: str
    signature_url: str

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValidationError("artifact URL must use HTTPS")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValidationError("artifact SHA-256 must be lowercase hexadecimal")
        validate_artifact_filename(self.filename, "artifact filename")
        if not self.signature_url.startswith("https://"):
            raise ValidationError("artifact signature URL must use HTTPS")


@dataclass(frozen=True)
class FirmwareRelease:
    """A firmware image and the exact SDK versions used to build it."""

    hardware: str
    ncs_version: str
    zephyr_version: str
    dfu_application_version: int
    artifact: Artifact
    release_url: str
    release_summary: str

    def __post_init__(self) -> None:
        validate_token(self.hardware, "hardware")
        validate_version(self.ncs_version, "ncs_version")
        validate_version(self.zephyr_version, "zephyr_version")
        validate_dfu_application_version(self.dfu_application_version, "dfu_application_version")
        if not self.release_url.startswith("https://"):
            raise ValidationError("release_url must use HTTPS")
        if len(self.release_summary) > 255:
            raise ValidationError("release_summary must fit the Home Assistant update entity")


@dataclass(frozen=True)
class NcpVersion:
    """Version information returned by standard SPINEL_PROP_NCP_VERSION."""

    raw: str
    hardware: str | None
    ncs_version: str | None
    zephyr_version: str | None


@dataclass(frozen=True)
class Settings:
    """Runtime settings delivered by Home Assistant Supervisor."""

    device: Path
    baudrate: int
    safe_update: bool
    qemu_usb_reenumeration_workaround: bool
    allow_legacy_rcp: bool
    allow_prereleases: bool
    pinned_ncs_minor: str | None
    dfu_serial_number: str | None
    dfu_usb_path: str | None
    manifest_poll_interval: int
    idle_window: int
    boot_timeout: int

    @classmethod
    def from_mapping(cls, options: dict[str, object]) -> Settings:
        device = _require_string(options.get("device"), "device")
        if not device.startswith("/dev/"):
            raise ValidationError("device must be a Home Assistant mapped /dev path")

        baudrate = options.get("baudrate", DEFAULT_BAUDRATE)
        if isinstance(baudrate, str) and baudrate.isascii() and baudrate.isdecimal():
            baudrate = int(baudrate)
        if not isinstance(baudrate, int) or baudrate not in {
            57600,
            115200,
            230400,
            460800,
            921600,
            1000000,
        }:
            raise ValidationError("baudrate is not supported by this app")

        manifest_poll_interval = options.get(
            "manifest_poll_interval", DEFAULT_MANIFEST_POLL_INTERVAL
        )
        idle_window = options.get("idle_window", DEFAULT_IDLE_WINDOW)
        boot_timeout = options.get("boot_timeout", DEFAULT_BOOT_TIMEOUT)
        if (
            not isinstance(manifest_poll_interval, int)
            or not 300 <= manifest_poll_interval <= 86400
        ):
            raise ValidationError("manifest_poll_interval must be between 300 and 86400 seconds")
        if not isinstance(idle_window, int) or not 10 <= idle_window <= 300:
            raise ValidationError("idle_window must be between 10 and 300 seconds")
        if not isinstance(boot_timeout, int) or not 15 <= boot_timeout <= 120:
            raise ValidationError("boot_timeout must be between 15 and 120 seconds")

        def option_bool(name: str, default: bool | None = None) -> bool:
            value = options.get(name, default)
            if not isinstance(value, bool):
                raise ValidationError(f"{name} must be a boolean")
            return value

        serial_number = _optional_string(options.get("dfu_serial_number"), "dfu_serial_number")
        if serial_number is not None:
            serial_number = validate_token(serial_number, "dfu_serial_number")
        usb_path = _optional_string(options.get("dfu_usb_path"), "dfu_usb_path")
        if usb_path is not None:
            usb_path = validate_usb_topology(usb_path, "dfu_usb_path")
        allow_legacy_rcp = option_bool("allow_legacy_rcp", default=DEFAULT_ALLOW_LEGACY_RCP)
        pinned_minor = _optional_string(options.get("pinned_ncs_minor"), "pinned_ncs_minor")
        if pinned_minor is not None:
            pinned_minor = validate_minor_line(pinned_minor, "pinned_ncs_minor")

        return cls(
            device=Path(device),
            baudrate=baudrate,
            safe_update=option_bool("safe_update", default=DEFAULT_SAFE_UPDATE),
            qemu_usb_reenumeration_workaround=option_bool(
                "qemu_usb_reenumeration_workaround",
                default=DEFAULT_QEMU_USB_REENUMERATION_WORKAROUND,
            ),
            allow_legacy_rcp=allow_legacy_rcp,
            allow_prereleases=option_bool(
                "allow_prereleases", default=DEFAULT_ALLOW_PRERELEASES
            ),
            pinned_ncs_minor=pinned_minor,
            dfu_serial_number=serial_number,
            dfu_usb_path=usb_path,
            manifest_poll_interval=manifest_poll_interval,
            idle_window=idle_window,
            boot_timeout=boot_timeout,
        )


def _optional_string(value: object, name: str) -> str | None:
    """Normalize omitted or blank Supervisor values before validation."""

    if value is None or value == "":
        return None
    return _require_string(value, name)
