"""Bounded validation and safe acquisition of manually supplied RCP ELFs."""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit, urlunsplit

from .manifest import ManifestError, validate_rcp_elf
from .models import (
    SUPPORTED_HARDWARE,
    PreparedFirmware,
    ValidationError,
    ncs_dfu_application_version,
    validate_version,
)

_MAX_URL_LENGTH = 2048
_MAX_REDIRECTS = 3
_DOWNLOAD_TIMEOUT = 20
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
# A PCA10059 application has less than 1 MiB of flash. This leaves generous
# room for ELF metadata while keeping buffered Home Assistant Ingress uploads
# bounded well below large-body proxy limits.
MAX_MANUAL_ARTIFACT_BYTES = 4 * 1024 * 1024
_PLATFORM_TAG = re.compile(
    rb"NRF52840 PCA10059 N/(?P<ncs>[A-Za-z0-9._+-]{1,80}) "
    rb"Z/(?P<zephyr>[A-Za-z0-9._+-]{1,80})"
)


class ManualArtifactError(RuntimeError):
    """A user-supplied firmware artifact or URL was rejected before DFU."""


@dataclass(frozen=True)
class DownloadResult:
    """One downloaded file plus a non-sensitive display form of its final URL."""

    path: Path
    display_url: str


@dataclass(frozen=True)
class _UrlTarget:
    url: str
    host: str
    port: int
    request_target: str
    display_url: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that uses an already screened DNS result exactly once."""

    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port=port, timeout=_DOWNLOAD_TIMEOUT, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def write_upload(stream: BinaryIO, content_length: int, directory: Path = Path("/tmp")) -> Path:
    """Store a raw upload under a server-generated name with a strict size bound."""

    if not 1 <= content_length <= MAX_MANUAL_ARTIFACT_BYTES:
        raise ManualArtifactError(
            f"uploaded firmware must be between 1 byte and {MAX_MANUAL_ARTIFACT_BYTES} bytes"
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="ingress-", suffix=".elf", dir=directory)
    path = Path(temporary_name)
    remaining = content_length
    try:
        with os.fdopen(descriptor, "wb") as destination:
            while remaining:
                read_size = min(_DOWNLOAD_CHUNK_SIZE, remaining)
                chunk = stream.read(read_size)
                if not isinstance(chunk, bytes) or not chunk:
                    raise ManualArtifactError("firmware upload ended before its declared size")
                if len(chunk) > read_size:
                    raise ManualArtifactError("firmware upload exceeded its declared size")
                destination.write(chunk)
                remaining -= len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def download_https_artifact(url: object, directory: Path = Path("/tmp")) -> DownloadResult:
    """Download a bounded HTTPS artifact without proxy use, SSRF, or DNS rebinding."""

    current = _parse_https_url(url)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        addresses = _resolve_global_addresses(current.host, current.port)
        response, connection = _request_once(current, addresses)
        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if redirect_count == _MAX_REDIRECTS:
                    raise ManualArtifactError("firmware URL exceeded the redirect limit")
                if not location or len(location) > _MAX_URL_LENGTH:
                    raise ManualArtifactError("firmware URL returned an invalid redirect")
                current = _parse_https_url(urljoin(current.url, location))
                continue
            if response.status != 200:
                raise ManualArtifactError(f"firmware URL returned HTTP {response.status}")
            return DownloadResult(_write_response(response, directory), current.display_url)
        finally:
            response.close()
            connection.close()
    raise AssertionError("bounded redirect loop did not return or raise")  # pragma: no cover


def prepare_manual_firmware(path: Path, source: str) -> PreparedFirmware:
    """Validate a bounded tagged PCA10059 ELF and derive its DFU version safely."""

    if source not in {"url", "upload"}:
        raise ManualArtifactError("manual firmware source is unsupported")
    try:
        size = path.stat().st_size
    except OSError as err:
        raise ManualArtifactError("manual firmware file is unavailable") from err
    if not 1 <= size <= MAX_MANUAL_ARTIFACT_BYTES:
        raise ManualArtifactError(
            f"manual firmware must be between 1 byte and {MAX_MANUAL_ARTIFACT_BYTES} bytes"
        )
    try:
        data = path.read_bytes()
    except OSError as err:
        raise ManualArtifactError("manual firmware file cannot be read") from err
    if len(data) != size:
        raise ManualArtifactError("manual firmware changed while it was being validated")
    try:
        validate_rcp_elf(data)
    except ManifestError as err:
        raise ManualArtifactError(str(err)) from err
    ncs_version, zephyr_version = _embedded_versions(data)
    try:
        application_version = ncs_dfu_application_version(ncs_version)
    except ValidationError as err:
        raise ManualArtifactError(str(err)) from err
    return PreparedFirmware(
        path=path,
        hardware=SUPPORTED_HARDWARE,
        ncs_version=ncs_version,
        zephyr_version=zephyr_version,
        dfu_application_version=application_version,
        sha256=sha256(data).hexdigest(),
        size=size,
        source=source,
    )


def redact_url(url: str) -> str:
    """Return a useful URL label without leaking signed-query credentials to UI logs."""

    target = _parse_https_url(url)
    return target.display_url


def _embedded_versions(data: bytes) -> tuple[str, str]:
    matches = {
        (match.group("ncs").decode("ascii"), match.group("zephyr").decode("ascii"))
        for match in _PLATFORM_TAG.finditer(data)
    }
    if len(matches) != 1:
        raise ManualArtifactError(
            "firmware must contain exactly one NRF52840 PCA10059 N/<ncs> Z/<zephyr> platform tag"
        )
    ncs_version, zephyr_version = matches.pop()
    try:
        return (
            validate_version(ncs_version, "embedded NCS version"),
            validate_version(zephyr_version, "embedded Zephyr version"),
        )
    except ValidationError as err:
        raise ManualArtifactError(str(err)) from err


def _parse_https_url(value: object) -> _UrlTarget:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH:
        raise ManualArtifactError("firmware URL must be a non-empty HTTPS URL under 2048 characters")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ManualArtifactError("firmware URL contains unsupported whitespace or control characters")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as err:
        raise ManualArtifactError("firmware URL has an invalid port") from err
    if parts.scheme.lower() != "https":
        raise ManualArtifactError("firmware URL must use HTTPS")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ManualArtifactError("firmware URL must include a hostname and no user credentials")
    if port not in {None, 443}:
        raise ManualArtifactError("firmware URL must use the HTTPS default port")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError as err:
        raise ManualArtifactError("firmware URL hostname is invalid") from err
    host_for_url = f"[{host}]" if ":" in host else host
    normalized_port = 443 if port is None else port
    path = parts.path or "/"
    request_target = urlunsplit(("", "", path, parts.query, ""))
    normalized = urlunsplit(("https", host_for_url, path, parts.query, ""))
    display_url = urlunsplit(("https", host_for_url, path, "", ""))
    return _UrlTarget(normalized, host, normalized_port, request_target, display_url)


def _resolve_global_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as err:
        raise ManualArtifactError("firmware URL hostname could not be resolved") from err
    addresses: list[str] = []
    for _family, _socktype, _protocol, _canonname, sockaddr in results:
        address = sockaddr[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not parsed.is_global:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ManualArtifactError("firmware URL must not resolve to a local or private address")
    return tuple(addresses)


def _request_once(
    target: _UrlTarget, addresses: tuple[str, ...]
) -> tuple[http.client.HTTPResponse, _PinnedHTTPSConnection]:
    last_error: OSError | http.client.HTTPException | ssl.SSLError | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(target.host, target.port, address)
        try:
            connection.request(
                "GET",
                target.request_target,
                headers={
                    "Accept": "application/octet-stream",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ha-pca10059-rcp-updater/0.1",
                },
            )
            return connection.getresponse(), connection
        except (OSError, http.client.HTTPException, ssl.SSLError) as err:
            last_error = err
            connection.close()
    raise ManualArtifactError("firmware URL could not be reached over HTTPS") from last_error


def _write_response(response: http.client.HTTPResponse, directory: Path) -> Path:
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as err:
            raise ManualArtifactError("firmware URL returned an invalid Content-Length") from err
        if declared_length < 0 or declared_length > MAX_MANUAL_ARTIFACT_BYTES:
            raise ManualArtifactError(
                f"firmware URL exceeds the {MAX_MANUAL_ARTIFACT_BYTES}-byte limit"
            )
    descriptor, temporary_name = tempfile.mkstemp(prefix="ingress-", suffix=".elf", dir=directory)
    path = Path(temporary_name)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as destination:
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MANUAL_ARTIFACT_BYTES:
                    raise ManualArtifactError(
                        f"firmware URL exceeds the {MAX_MANUAL_ARTIFACT_BYTES}-byte limit"
                    )
                destination.write(chunk)
            if total == 0:
                raise ManualArtifactError("firmware URL returned an empty file")
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise
