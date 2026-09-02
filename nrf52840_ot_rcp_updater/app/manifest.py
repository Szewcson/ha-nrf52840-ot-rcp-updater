"""Release-manifest parsing and verified artifact downloads."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen
import zipfile

from .models import Artifact, FirmwareRelease, ValidationError, validate_token, validate_version


MAX_MANIFEST_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_FILES = 16
MAX_ARTIFACT_UNCOMPRESSED_BYTES = 16 * 1024 * 1024


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

    def newest_for(self, hardware: str) -> FirmwareRelease:
        candidates = [release for release in self._releases if release.hardware == hardware]
        if not candidates:
            raise ManifestError(f"no supported release for hardware {hardware}")
        # The NCP exposes NCS, not this project's release label, over Spinel.
        return max(candidates, key=lambda release: _version_key(release.ncs_version))


def _version_key(version: str) -> tuple[int, ...]:
    base = version.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in base.split("."))


def download_artifact(release: FirmwareRelease, destination_directory: Path) -> Path:
    """Download one manifest-selected package, atomically, after hashing it."""

    destination_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination_directory / release.artifact.filename
    if target.is_file() and target.stat().st_size <= MAX_ARTIFACT_BYTES:
        cached = target.read_bytes()
        if sha256(cached).hexdigest() == release.artifact.sha256:
            _validate_dfu_zip(cached)
            return target

    data = _fetch(release.artifact.url, MAX_ARTIFACT_BYTES)
    digest = sha256(data).hexdigest()
    if digest != release.artifact.sha256:
        raise ManifestError("firmware package SHA-256 does not match its release manifest")
    _validate_dfu_zip(data)

    descriptor, temporary_name = tempfile.mkstemp(prefix="download-", suffix=".zip", dir=destination_directory)
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


def _validate_dfu_zip(data: bytes) -> None:
    """Reject ZIP bombs and path-shaped input before passing it to nrfutil."""

    if not data.startswith(b"PK\x03\x04"):
        raise ManifestError("firmware artifact is not a ZIP file")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as err:
        raise ManifestError("firmware artifact is not a valid ZIP file") from err
    if not 1 <= len(entries) <= MAX_ARTIFACT_FILES:
        raise ManifestError("firmware artifact has an unsafe number of ZIP entries")

    total_size = 0
    names: set[str] = set()
    for entry in entries:
        name = entry.filename
        if name.startswith("/") or "\\" in name or ".." in name.split("/"):
            raise ManifestError("firmware artifact contains an unsafe ZIP path")
        if entry.is_dir():
            continue
        total_size += entry.file_size
        names.add(name)
    if total_size > MAX_ARTIFACT_UNCOMPRESSED_BYTES:
        raise ManifestError("firmware artifact expands beyond the configured size limit")
    if "manifest.json" not in names:
        raise ManifestError("firmware artifact does not contain a Nordic DFU manifest")
