"""Install reviewed custom notices into NCS's disposable SBOM text database."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


class NcsSbomLicenseTextError(RuntimeError):
    """NCS's SBOM license-text database cannot be safely extended."""


_MAX_DATABASE_BYTES = 4 * 1024 * 1024
_MAX_ADDITIONS_BYTES = 16 * 1024
_LICENSE_ID = re.compile(r"^- id:\s*(LicenseRef-[A-Za-z0-9.-]+)\s*$", re.MULTILINE)
_REQUIRED_IDS = frozenset(
    {
        "licenseref-scancode-delorie-historical",
        "licenseref-scancode-red-hat-attribution",
    }
)


def _read_regular_file(path: Path, maximum_size: int, description: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise NcsSbomLicenseTextError(f"{description} must be a regular file")
        if path.stat().st_size > maximum_size:
            raise NcsSbomLicenseTextError(f"{description} exceeds {maximum_size} bytes")
        return path.read_bytes()
    except OSError as err:
        raise NcsSbomLicenseTextError(f"cannot read {description}: {err}") from err


def _license_ids(content: str, description: str, *, require_unique: bool) -> frozenset[str]:
    ids = [match.casefold() for match in _LICENSE_ID.findall(content)]
    if require_unique and len(ids) != len(set(ids)):
        raise NcsSbomLicenseTextError(f"{description} has duplicate license IDs")
    return frozenset(ids)


def install_license_texts(database_path: Path, additions_path: Path) -> bool:
    """Append reviewed notices once, or accept an NCS release that includes both.

    NCS's ScanCode adapter reports these two custom identifiers but (in the
    affected releases) does not retain their matched text. Adding them to the
    tool's own database makes the full-text detector retain the exact notices
    and makes its SPDX/HTML writers emit complete custom-license records.
    """

    database = _read_regular_file(database_path, _MAX_DATABASE_BYTES, "NCS SBOM database")
    additions = _read_regular_file(additions_path, _MAX_ADDITIONS_BYTES, "SBOM notice additions")
    try:
        database_text = database.decode("utf-8")
        additions_text = additions.decode("utf-8")
    except UnicodeDecodeError as err:
        raise NcsSbomLicenseTextError("SBOM license-text inputs must be UTF-8") from err

    addition_ids = _license_ids(additions_text, "SBOM notice additions", require_unique=True)
    if addition_ids != _REQUIRED_IDS:
        raise NcsSbomLicenseTextError("SBOM notice additions must define exactly the reviewed IDs")

    present_ids = _license_ids(
        database_text, "NCS SBOM database", require_unique=False
    ) & _REQUIRED_IDS
    if present_ids == _REQUIRED_IDS:
        return False
    if present_ids:
        raise NcsSbomLicenseTextError(
            "NCS SBOM database defines only part of the reviewed custom-license set"
        )

    replacement = database + (b"" if database.endswith(b"\n") else b"\n") + additions
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="license-texts-", suffix=".yaml", dir=database_path.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, database_path.stat().st_mode & 0o777)
        os.replace(temporary_name, database_path)
    except OSError as err:
        raise NcsSbomLicenseTextError(f"cannot update NCS SBOM database: {err}") from err
    finally:
        try:
            os.unlink(temporary_name)
        except (FileNotFoundError, UnboundLocalError):
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--additions", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        changed = install_license_texts(arguments.database, arguments.additions)
    except NcsSbomLicenseTextError as err:
        parser.error(str(err))
    if changed:
        print("Installed reviewed custom license texts into NCS SBOM database")
    else:
        print("NCS SBOM database already provides the reviewed custom license texts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
