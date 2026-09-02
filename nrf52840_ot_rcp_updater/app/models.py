"""Validated data exchanged by the updater components."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


class ValidationError(ValueError):
    """Raised when external configuration or release metadata is unsafe."""


_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ADDON_SLUG_RE = re.compile(r"^[a-z0-9_]{1,128}$")


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def validate_version(value: object, name: str) -> str:
    version = _require_string(value, name)
    if not _VERSION_RE.fullmatch(version):
        raise ValidationError(f"{name} has an invalid version format: {version!r}")
    return version


def validate_token(value: object, name: str) -> str:
    token = _require_string(value, name)
    if not _TOKEN_RE.fullmatch(token):
        raise ValidationError(f"{name} contains unsupported characters: {token!r}")
    return token


@dataclass(frozen=True)
class Artifact:
    """An immutable Nordic DFU package selected from a release manifest."""

    url: str
    sha256: str
    filename: str

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValidationError("artifact URL must use HTTPS")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValidationError("artifact SHA-256 must be lowercase hexadecimal")
        if "/" in self.filename or self.filename in {"", ".", ".."}:
            raise ValidationError("artifact filename must not contain a path")
        if not self.filename.endswith(".zip"):
            raise ValidationError("artifact must be a Nordic DFU .zip package")


@dataclass(frozen=True)
class FirmwareRelease:
    """A firmware image and the exact SDK versions used to build it."""

    hardware: str
    ncs_version: str
    zephyr_version: str
    artifact: Artifact
    release_url: str
    release_summary: str

    def __post_init__(self) -> None:
        validate_token(self.hardware, "hardware")
        validate_version(self.ncs_version, "ncs_version")
        validate_version(self.zephyr_version, "zephyr_version")
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
    hardware: str
    otbr_addon_slug: str
    otbr_api_url: str | None
    safe_update: bool
    allow_legacy_rcp: bool
    dfu_serial_number: str | None
    manifest_url: str | None
    manifest_poll_interval: int
    idle_window: int
    boot_timeout: int

    @classmethod
    def from_mapping(cls, options: dict[str, object]) -> Settings:
        device = _require_string(options.get("device"), "device")
        if not device.startswith("/dev/"):
            raise ValidationError("device must be a Home Assistant mapped /dev path")

        baudrate = options.get("baudrate")
        if not isinstance(baudrate, int) or baudrate not in {
            115200,
            230400,
            460800,
            921600,
            1000000,
        }:
            raise ValidationError("baudrate is not supported by this app")

        hardware = validate_token(options.get("hardware"), "hardware")
        addon_slug = _require_string(options.get("otbr_addon_slug"), "otbr_addon_slug")
        if not _ADDON_SLUG_RE.fullmatch(addon_slug):
            raise ValidationError("otbr_addon_slug has an invalid format")

        api_url = options.get("otbr_api_url")
        if api_url is not None:
            api_url = _require_string(api_url, "otbr_api_url").rstrip("/")
            if not api_url.startswith("http://") and not api_url.startswith("https://"):
                raise ValidationError("otbr_api_url must use HTTP or HTTPS")

        manifest_url = options.get("manifest_url")
        if manifest_url is not None:
            manifest_url = _require_string(manifest_url, "manifest_url")
            if not manifest_url.startswith("https://"):
                raise ValidationError("manifest_url must use HTTPS")

        manifest_poll_interval = options.get("manifest_poll_interval")
        idle_window = options.get("idle_window")
        boot_timeout = options.get("boot_timeout")
        if not isinstance(manifest_poll_interval, int) or not 300 <= manifest_poll_interval <= 86400:
            raise ValidationError("manifest_poll_interval must be between 300 and 86400 seconds")
        if not isinstance(idle_window, int) or not 10 <= idle_window <= 300:
            raise ValidationError("idle_window must be between 10 and 300 seconds")
        if not isinstance(boot_timeout, int) or not 15 <= boot_timeout <= 120:
            raise ValidationError("boot_timeout must be between 15 and 120 seconds")

        def option_bool(name: str) -> bool:
            value = options.get(name)
            if not isinstance(value, bool):
                raise ValidationError(f"{name} must be a boolean")
            return value

        serial_number = options.get("dfu_serial_number")
        if serial_number is not None:
            serial_number = validate_token(serial_number, "dfu_serial_number")

        return cls(
            device=Path(device),
            baudrate=baudrate,
            hardware=hardware,
            otbr_addon_slug=addon_slug,
            otbr_api_url=api_url,
            safe_update=option_bool("safe_update"),
            allow_legacy_rcp=option_bool("allow_legacy_rcp"),
            dfu_serial_number=serial_number,
            manifest_url=manifest_url,
            manifest_poll_interval=manifest_poll_interval,
            idle_window=idle_window,
            boot_timeout=boot_timeout,
        )
