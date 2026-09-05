"""Release-manifest parsing and verified artifact downloads."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from hashlib import sha256
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import (
    Artifact,
    FirmwareRelease,
    ValidationError,
    is_prerelease,
    minor_line,
    validate_dfu_application_version,
    validate_token,
    validate_version,
    version_key,
)

MAX_MANIFEST_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_ELF32_HEADER_SIZE = 52
_ELF32_PROGRAM_HEADER_SIZE = 32
_ELF32_EM_ARM = 40
_ELF32_PT_LOAD = 1
_PCA10059_APPLICATION_START = 0x1000
_PCA10059_FLASH_END = 0x00100000


class ManifestError(RuntimeError):
    """A release manifest or a release artifact could not be trusted."""


def _fetch(url: str, maximum_size: int) -> bytes:
    request = Request(url, headers={"User-Agent": "ha-nrf52840-ot-rcp-updater/0.1"})
    try:
        with urlopen(request, timeout=20) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > maximum_size:
                raise ManifestError(f"download from {url} exceeds the configured size limit")
            data = response.read(maximum_size + 1)
    except (OSError, URLError, ValueError) as err:
        raise ManifestError(f"unable to download {url}: {err}") from err
    if len(data) > maximum_size:
        raise ManifestError(f"download from {url} exceeds the configured size limit")
    return data


class FirmwareManifest:
    """An allow-list of verified PCA10059 firmware artifacts."""

    def __init__(self, releases: tuple[FirmwareRelease, ...]) -> None:
        self._releases = releases

    @classmethod
    def from_bytes(cls, payload: bytes) -> FirmwareManifest:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ManifestError("release manifest is not valid UTF-8 JSON") from err
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ManifestError("release manifest schema_version must be 1")

        items = document.get("releases")
        if not isinstance(items, list):
            raise ManifestError("release manifest releases must be an array")

        releases: list[FirmwareRelease] = []
        for item in items:
            if not isinstance(item, dict):
                raise ManifestError("release entries must be objects")
            artifact_item = item.get("artifact")
            if not isinstance(artifact_item, dict):
                raise ManifestError("release artifact must be an object")
            try:
                artifact = Artifact(
                    url=str(artifact_item["url"]),
                    sha256=str(artifact_item["sha256"]),
                    filename=str(artifact_item["filename"]),
                )
                release = FirmwareRelease(
                    hardware=validate_token(item["hardware"], "hardware"),
                    ncs_version=validate_version(item["ncs_version"], "ncs_version"),
                    zephyr_version=validate_version(item["zephyr_version"], "zephyr_version"),
                    dfu_application_version=validate_dfu_application_version(
                        item["dfu_application_version"], "dfu_application_version"
                    ),
                    artifact=artifact,
                    release_url=str(item["release_url"]),
                    release_summary=str(item["release_summary"]),
                )
            except (KeyError, ValidationError) as err:
                raise ManifestError(f"invalid firmware release: {err}") from err
            releases.append(release)
        return cls(tuple(releases))

    @classmethod
    def download(cls, url: str) -> FirmwareManifest:
        return cls.from_bytes(_fetch(url, MAX_MANIFEST_BYTES))

    def newest_for(
        self,
        hardware: str,
        allow_prereleases: bool = False,
        pinned_minor: str | None = None,
    ) -> FirmwareRelease:
        """Select the newest allowed release without crossing a policy boundary."""

        candidates = [
            release
            for release in self.releases_for(hardware, allow_prereleases=allow_prereleases)
            if pinned_minor is None or minor_line(release.ncs_version) == pinned_minor
        ]
        if not candidates:
            raise ManifestError(
                "no release matches the configured NCS channel and minor-line policy"
            )
        return max(candidates, key=lambda release: version_key(release.ncs_version))

    def releases_for(
        self, hardware: str, allow_prereleases: bool = False
    ) -> tuple[FirmwareRelease, ...]:
        """Return manifest-verified targets for the runtime firmware selector."""

        return tuple(
            sorted(
                (
                    release
                    for release in self._releases
                    if release.hardware == hardware
                    and (allow_prereleases or not is_prerelease(release.ncs_version))
                ),
                key=lambda release: version_key(release.ncs_version),
            )
        )

    def release_for(self, hardware: str, ncs_version: str) -> FirmwareRelease:
        """Return one configured legacy-migration target from the trusted manifest."""

        matches = [
            release
            for release in self._releases
            if release.hardware == hardware and release.ncs_version == ncs_version
        ]
        if len(matches) != 1:
            raise ManifestError(f"no unique {hardware} release for NCS {ncs_version}")
        return matches[0]


def download_artifact(release: FirmwareRelease, destination_directory: Path) -> Path:
    """Download one manifest-selected ELF, atomically, after hashing it."""

    destination_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination_directory / release.artifact.filename
    if target.is_file() and target.stat().st_size <= MAX_ARTIFACT_BYTES:
        cached = target.read_bytes()
        if sha256(cached).hexdigest() == release.artifact.sha256:
            _validate_rcp_elf(cached)
            return target

    data = _fetch(release.artifact.url, MAX_ARTIFACT_BYTES)
    digest = sha256(data).hexdigest()
    if digest != release.artifact.sha256:
        raise ManifestError("firmware ELF SHA-256 does not match its release manifest")
    _validate_rcp_elf(data)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="download-", suffix=".elf", dir=destination_directory
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return target


def _validate_rcp_elf(data: bytes) -> None:
    """Accept only a bounded 32-bit little-endian ARM ELF in PCA10059 flash."""

    if len(data) < _ELF32_HEADER_SIZE:
        raise ManifestError("firmware artifact is too short to be an ELF32 image")
    if data[:4] != b"\x7fELF":
        raise ManifestError("firmware artifact is not an ELF file")
    if data[4:7] != b"\x01\x01\x01":
        raise ManifestError("firmware artifact must be a 32-bit little-endian ELF")

    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    if e_type != 2 or e_machine != _ELF32_EM_ARM:
        raise ManifestError("firmware artifact must be an ARM executable ELF")
    program_offset = struct.unpack_from("<I", data, 28)[0]
    program_entry_size, program_count = struct.unpack_from("<HH", data, 42)
    if program_entry_size < _ELF32_PROGRAM_HEADER_SIZE or program_count == 0:
        raise ManifestError("firmware ELF has no valid program headers")
    table_size = program_entry_size * program_count
    if program_offset > len(data) or table_size > len(data) - program_offset:
        raise ManifestError("firmware ELF program headers are outside the file")

    has_flash_segment = False
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        segment_type, file_offset, _, physical_address, file_size = struct.unpack_from(
            "<IIIII", data, offset
        )
        if segment_type != _ELF32_PT_LOAD or file_size == 0:
            continue
        if file_offset > len(data) or file_size > len(data) - file_offset:
            raise ManifestError("firmware ELF load segment is outside the file")
        # Embedded ELF files commonly map their ELF headers at address zero.
        # nrfdfu-rs ignores this non-firmware segment before emitting flash data.
        if physical_address == 0:
            continue
        if (
            physical_address < _PCA10059_APPLICATION_START
            or physical_address > _PCA10059_FLASH_END - file_size
        ):
            raise ManifestError("firmware ELF load segment is outside PCA10059 application flash")
        has_flash_segment = True
    if not has_flash_segment:
        raise ManifestError("firmware ELF has no loadable PCA10059 flash segment")
