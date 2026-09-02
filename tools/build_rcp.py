#!/usr/bin/env python3
"""Build a tagged PCA10059 OpenThread RCP and its Nordic DFU package.

The NCS tree is never edited.  The version tags are supplied as an additional
Kconfig fragment, so the stock `SPINEL_PROP_NCP_VERSION` string remains the
only protocol surface used by the updater.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class BuildError(RuntimeError):
    """The requested release cannot be built reproducibly."""


_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_ASSIGNMENT = re.compile(r"^\s*([A-Z_]+)\s*=\s*([^#\s]+)", re.MULTILINE)


def _read_ncs_version(ncs_root: Path) -> str:
    content = (ncs_root / "nrf" / "VERSION").read_text(encoding="utf-8").strip()
    match = re.search(r"\bVERSION\s*=\s*([0-9]+(?:\.[0-9]+){1,3})\b", content)
    if not match:
        match = re.search(r"\b([0-9]+(?:\.[0-9]+){1,3})\b", content)
    if not match:
        raise BuildError("nrf/VERSION does not contain a supported NCS version")
    return match.group(1)


def _read_zephyr_version(ncs_root: Path) -> str:
    assignments = dict(_ASSIGNMENT.findall((ncs_root / "zephyr" / "VERSION").read_text("utf-8")))
    try:
        parts = [assignments["VERSION_MAJOR"], assignments["VERSION_MINOR"], assignments["PATCHLEVEL"]]
    except KeyError as err:
        raise BuildError(f"zephyr/VERSION lacks {err.args[0]}") from err
    version = ".".join(parts)
    if not _VERSION.fullmatch(version):
        raise BuildError(f"zephyr/VERSION produced an invalid version: {version!r}")
    return version


def platform_info(ncs_version: str, zephyr_version: str) -> str:
    """Return the additive platform-info field supplied to OpenThread."""

    for value, name in ((ncs_version, "NCS version"), (zephyr_version, "Zephyr version")):
        if not _VERSION.fullmatch(value):
            raise BuildError(f"{name} is invalid: {value!r}")
    return f"HW/PCA10059 NCS/{ncs_version} ZEPHYR/{zephyr_version}"


def dfu_application_version(ncs_version: str) -> int:
    """Map an NCS semver to the monotonic integer expected by Nordic DFU."""

    if not _VERSION.fullmatch(ncs_version):
        raise BuildError(f"NCS version is invalid: {ncs_version!r}")
    parts = [int(part) for part in ncs_version.split(".")]
    parts.extend([0] * (3 - len(parts)))
    major, minor, patch = parts[:3]
    if any(part > 999 for part in (major, minor, patch)):
        raise BuildError("NCS version components must be at most 999")
    return major * 1_000_000 + minor * 1_000 + patch


def _run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    try:
        subprocess.run(command, cwd=cwd, env=environment, check=True)
    except FileNotFoundError as err:
        raise BuildError(f"required tool is not installed: {command[0]}") from err
    except subprocess.CalledProcessError as err:
        raise BuildError(f"command failed with exit status {err.returncode}: {' '.join(command)}") from err


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
    package = output_directory / f"nrf52840-ot-rcp-ncs-{actual_ncs_version}.zip"

    with tempfile.TemporaryDirectory(prefix="ot-rcp-kconfig-", dir=output_directory) as temporary_directory:
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

    configured = (build_directory / "zephyr" / ".config").read_text(encoding="utf-8")
    expected_config = f'CONFIG_OPENTHREAD_CONFIG_PLATFORM_INFO="{info}"'
    if expected_config not in configured:
        raise BuildError("generated Zephyr configuration did not retain the requested Spinel platform tags")

    image = build_directory / "zephyr" / "zephyr.hex"
    if not image.is_file():
        raise BuildError("west build did not produce zephyr.hex")
    _run(
        [
            "nrfutil",
            "nrf5sdk-tools",
            "pkg",
            "generate",
            "--hw-version",
            "52",
            "--sd-req=0x00",
            "--application-version",
            str(dfu_application_version(actual_ncs_version)),
            "--application",
            str(image),
            str(package),
        ],
        ncs_root,
        environment,
    )
    if not package.is_file() or not package.read_bytes().startswith(b"PK\x03\x04"):
        raise BuildError("nrfutil did not produce a Nordic DFU ZIP package")

    metadata = {
        "hardware": "PCA10059",
        "ncs_version": actual_ncs_version,
        "zephyr_version": zephyr_version,
        "platform_info": info,
        "artifact": package.name,
        "sha256": sha256(package.read_bytes()).hexdigest(),
    }
    (output_directory / "release-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return package


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
