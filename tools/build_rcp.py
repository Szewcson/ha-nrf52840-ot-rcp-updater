#!/usr/bin/env python3
"""Build a tagged PCA10059 OpenThread RCP and its Secure DFU ELF.

The NCS tree is never edited.  The version tags are supplied as an additional
Kconfig fragment, so the stock `SPINEL_PROP_NCP_VERSION` string remains the
only protocol surface used by the updater.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path


class BuildError(RuntimeError):
    """The requested release cannot be built reproducibly."""


_VERSION_TEXT = (
    r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"(?:-(?P<prerelease>(?:preview|rc)[1-9][0-9]*))?"
)
_VERSION = re.compile(rf"^{_VERSION_TEXT}$")
_NUMERIC_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# Keep optional values on their own line: an empty EXTRAVERSION must not consume
# the following VERSION_METADATA assignment.
_ASSIGNMENT = re.compile(r"^[ \t]*([A-Z_]+)[ \t]*=[ \t]*([^#\s]+)", re.MULTILINE)
_PLATFORM_SOC = "NRF52840"
_MAX_NCP_VERSION_BYTES = 127
_FORBIDDEN_PLATFORM_INFO_CHARACTERS = frozenset({";", "_"})
_PACKAGE_PREFIX = b"OPENTHREAD/"


def _read_ncs_version(ncs_root: Path) -> str:
    version_file = next(
        (
            candidate
            for candidate in (ncs_root / "VERSION", ncs_root / "nrf" / "VERSION")
            if candidate.is_file()
        ),
        None,
    )
    if version_file is None:
        raise BuildError("NCS checkout does not contain VERSION or nrf/VERSION")
    content = version_file.read_text(encoding="utf-8").strip()

    assignments = dict(_ASSIGNMENT.findall(content))
    if {"VERSION_MAJOR", "VERSION_MINOR", "PATCHLEVEL"} <= assignments.keys():
        version = ".".join(
            [assignments["VERSION_MAJOR"], assignments["VERSION_MINOR"], assignments["PATCHLEVEL"]]
        )
        extra = assignments.get("EXTRAVERSION", "")
        if extra:
            version = f"{version}-{extra.lstrip('-')}"
    else:
        match = re.search(rf"\bVERSION\s*=\s*({_VERSION_TEXT})\b", content)
        if not match:
            match = re.search(rf"\b({_VERSION_TEXT})\b", content)
        if not match:
            raise BuildError(
                f"{version_file.relative_to(ncs_root)} does not contain a supported NCS version"
            )
        version = match.group(1)
    if not _VERSION.fullmatch(version):
        raise BuildError(
            f"{version_file.relative_to(ncs_root)} produced an invalid NCS version: {version!r}"
        )
    return version


def _read_zephyr_version(ncs_root: Path) -> str:
    assignments = dict(_ASSIGNMENT.findall((ncs_root / "zephyr" / "VERSION").read_text("utf-8")))
    try:
        parts = [
            assignments["VERSION_MAJOR"],
            assignments["VERSION_MINOR"],
            assignments["PATCHLEVEL"],
        ]
    except KeyError as err:
        raise BuildError(f"zephyr/VERSION lacks {err.args[0]}") from err
    version = ".".join(parts)
    if not _NUMERIC_VERSION.fullmatch(version):
        raise BuildError(f"zephyr/VERSION produced an invalid version: {version!r}")
    return version


def platform_info(ncs_version: str, zephyr_version: str) -> str:
    """Return the additive platform-info field supplied to OpenThread.

    OpenThread leaves this field's syntax to the platform. Start with the
    upstream nRF convention so host tooling recognises the RCP before reading
    this project's compact additive tags. Their short names leave room for
    NCS-generated package versions and build timestamps within Spinel's fixed
    host-side version buffer.
    """

    if not _VERSION.fullmatch(ncs_version):
        raise BuildError(f"NCS version is invalid: {ncs_version!r}")
    if not _NUMERIC_VERSION.fullmatch(zephyr_version):
        raise BuildError(f"Zephyr version is invalid: {zephyr_version!r}")
    info = f"{_PLATFORM_SOC} PCA10059 N/{ncs_version} Z/{zephyr_version}"
    _validate_platform_info_value(info)
    return info


def _validate_platform_info_value(info: str) -> None:
    """Reject platform-info values unsafe for the shared NCP version string."""

    try:
        info.encode("ascii")
    except UnicodeEncodeError as err:
        raise BuildError("Spinel platform info must contain ASCII characters only") from err
    forbidden = "".join(sorted(set(info) & _FORBIDDEN_PLATFORM_INFO_CHARACTERS))
    if forbidden:
        raise BuildError(f"Spinel platform info must not contain: {forbidden!r}")
    if not info.startswith(f"{_PLATFORM_SOC} "):
        raise BuildError(f"Spinel platform info must start with {_PLATFORM_SOC}")


def dfu_application_version(ncs_version: str) -> int:
    """Map an NCS semver to the monotonic integer expected by Nordic DFU."""

    match = _VERSION.fullmatch(ncs_version)
    if match is None:
        raise BuildError(f"NCS version is invalid: {ncs_version!r}")
    major, minor, patch = [int(match.group(name)) for name in ("major", "minor", "patch")]
    if any(part > 999 for part in (major, minor, patch)):
        raise BuildError("NCS version components must be at most 999")
    prerelease = match.group("prerelease")
    if prerelease is None:
        stage = 99
    else:
        sequence_match = re.search(r"[0-9]+$", prerelease)
        assert sequence_match is not None
        sequence = int(sequence_match.group())
        if sequence > 49:
            raise BuildError("NCS prerelease sequence must be at most 49")
        # Reserve 1-49 for previews, 50-98 for RCs, and 99 for the final tag.
        stage = sequence if prerelease.startswith("preview") else 49 + sequence

    # Leave room for every release stage, including the next minor after a
    # high patch number. This is monotonic across the supported NCS semver form.
    application_version = (major * 1_000_000 + minor * 1_000 + patch) * 100 + stage
    if application_version > 0xFFFFFFFF:
        raise BuildError("NCS version cannot be represented by Secure DFU")
    return application_version


def _run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    try:
        subprocess.run(command, cwd=cwd, env=environment, check=True)
    except FileNotFoundError as err:
        raise BuildError(f"required tool is not installed: {command[0]}") from err
    except subprocess.CalledProcessError as err:
        raise BuildError(
            f"command failed with exit status {err.returncode}: {' '.join(command)}"
        ) from err


def _rcp_output(build_directory: Path, filename: str) -> Path:
    """Locate an RCP output from sysbuild or the older single-image layout."""

    for output in (
        build_directory / "coprocessor" / "zephyr" / filename,
        build_directory / "zephyr" / filename,
    ):
        if output.is_file():
            return output
    raise BuildError(f"west build did not produce the RCP output {filename}")


def _validate_platform_info(build_directory: Path, info: str) -> None:
    configured = _rcp_output(build_directory, ".config").read_text(encoding="utf-8")
    expected_config = f'CONFIG_OPENTHREAD_CONFIG_PLATFORM_INFO="{info}"'
    if expected_config not in configured:
        raise BuildError(
            "generated RCP configuration did not retain the requested Spinel platform tags"
        )


def _validate_platform_info_binary(image: Path, info: str) -> str:
    """Return and validate the exact NCP version composed in the RCP ELF.

    OpenThread composes ``PACKAGE_NAME/PACKAGE_VERSION; PLATFORM_INFO`` and an
    optional ``; BUILD_DATETIME`` at compile time. The host Spinel driver stores
    that *full* NUL-terminated value in ``char mVersion[128]``. Inspecting the
    ELF is the only reliable way to budget NCS's generated package version and
    build date together with our platform field.
    """

    info_bytes = info.encode("ascii")
    image_bytes = image.read_bytes()
    marker = b"; " + info_bytes
    matches: set[bytes] = set()
    offset = 0

    while True:
        offset = image_bytes.find(marker, offset)
        if offset < 0:
            break
        start = offset
        while start > 0 and 0x20 <= image_bytes[start - 1] <= 0x7E:
            start -= 1
        end = image_bytes.find(b"\0", offset)
        if end >= 0:
            candidate = image_bytes[start:end]
            fields = candidate.split(b"; ")
            if (
                len(fields) in (2, 3)
                and fields[0].startswith(_PACKAGE_PREFIX)
                and fields[1] == info_bytes
            ):
                matches.add(candidate)
        offset += len(marker)

    if not matches:
        raise BuildError(
            "compiled RCP image did not retain a complete OpenThread NCP version string"
        )
    if len(matches) != 1:
        raise BuildError("compiled RCP image contains ambiguous OpenThread NCP version strings")

    version = matches.pop()
    if any(not 0x20 <= byte <= 0x7E for byte in version):
        raise BuildError("compiled OpenThread NCP version string is not printable ASCII")
    if len(version) > _MAX_NCP_VERSION_BYTES:
        raise BuildError(
            "compiled OpenThread NCP version string is "
            f"{len(version)} bytes; the host Spinel limit is {_MAX_NCP_VERSION_BYTES} bytes"
        )
    return version.decode("ascii")


def build(ncs_root: Path, expected_ncs_version: str, output_directory: Path) -> Path:
    ncs_root = ncs_root.resolve()
    sample = ncs_root / "nrf" / "samples" / "openthread" / "coprocessor"
    if not sample.is_dir() or not (ncs_root / "zephyr").is_dir():
        raise BuildError("ncs_root must contain nrf and zephyr from a complete NCS checkout")

    actual_ncs_version = _read_ncs_version(ncs_root)
    if actual_ncs_version != expected_ncs_version:
        raise BuildError(
            f"NCS checkout is {actual_ncs_version}, expected {expected_ncs_version}; refusing an unpinned build"
        )
    zephyr_version = _read_zephyr_version(ncs_root)
    info = platform_info(actual_ncs_version, zephyr_version)
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    build_directory = output_directory / "build"
    artifact = output_directory / f"nrf52840-ot-rcp-ncs-{actual_ncs_version}.elf"

    with tempfile.TemporaryDirectory(
        prefix="ot-rcp-kconfig-", dir=output_directory
    ) as temporary_directory:
        kconfig = Path(temporary_directory) / "platform-info.conf"
        kconfig.write_text(f'CONFIG_OPENTHREAD_CONFIG_PLATFORM_INFO="{info}"\n', encoding="utf-8")
        environment = {**os.environ, "ZEPHYR_BASE": str(ncs_root / "zephyr")}
        _run(
            [
                "west",
                "build",
                "--pristine",
                "always",
                "--board",
                "nrf52840dongle/nrf52840",
                "--build-dir",
                str(build_directory),
                str(sample),
                "--",
                f"-DEXTRA_CONF_FILE={kconfig}",
            ],
            ncs_root,
            environment,
        )

    _validate_platform_info(build_directory, info)
    image = _rcp_output(build_directory, "zephyr.elf")
    composed_version = _validate_platform_info_binary(image, info)
    shutil.copyfile(image, artifact)
    if not artifact.is_file() or not artifact.read_bytes().startswith(b"\x7fELF"):
        raise BuildError("west build did not produce an ELF firmware artifact")

    metadata = {
        "hardware": "PCA10059",
        "ncs_version": actual_ncs_version,
        "zephyr_version": zephyr_version,
        "platform_info": info,
        "ncp_version": composed_version,
        "ncp_version_bytes": len(composed_version.encode("ascii")),
        "dfu_application_version": dfu_application_version(actual_ncs_version),
        "artifact": artifact.name,
        "sha256": sha256(artifact.read_bytes()).hexdigest(),
    }
    (output_directory / "release-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ncs-root", required=True, type=Path)
    parser.add_argument("--expected-ncs-version", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        package = build(arguments.ncs_root, arguments.expected_ncs_version, arguments.output_dir)
    except (BuildError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
