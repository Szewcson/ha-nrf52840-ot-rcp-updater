"""Fail publication when an NCS SPDX report leaves a used-file license unknown."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


class SbomError(RuntimeError):
    """The generated SBOM is incomplete for unattended publication."""


_MAX_SBOM_BYTES = 32 * 1024 * 1024
_LICENSE_LINE = re.compile(r"^FileLicenseConcluded:\s*(.+?)\s*$", re.MULTILINE)
_UNKNOWN_LICENSE_MARKERS = ("NOASSERTION", "NONE", "LicenseRef-Unknown")


def validate_spdx(path: Path) -> None:
    """Require NCS SBOM to conclude a non-placeholder license per used file.

    NCS marks build-directory analysis as experimental. An unattended publisher
    must therefore stop instead of silently shipping a release if the produced
    report contains a file whose concluded license is unknown.
    """

    try:
        if path.stat().st_size > _MAX_SBOM_BYTES:
            raise SbomError(f"SBOM exceeds {_MAX_SBOM_BYTES} bytes")
        document = path.read_text(encoding="utf-8")
    except OSError as err:
        raise SbomError(f"cannot read SPDX SBOM: {err}") from err
    licenses = _LICENSE_LINE.findall(document)
    if not licenses:
        raise SbomError("SPDX SBOM contains no FileLicenseConcluded entries")
    unknown = [
        license_expression
        for license_expression in licenses
        if any(marker in license_expression for marker in _UNKNOWN_LICENSE_MARKERS)
    ]
    if unknown:
        examples = ", ".join(sorted(set(unknown))[:4])
        raise SbomError(
            "SPDX SBOM has unknown concluded file licenses; refusing unattended publication: "
            f"{examples}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spdx", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        validate_spdx(arguments.spdx)
    except SbomError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
