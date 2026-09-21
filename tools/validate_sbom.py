"""Fail publication when an NCS SPDX report leaves a used-file license unknown."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


class SbomError(RuntimeError):
    """The generated SBOM is incomplete for unattended publication."""


_MAX_SBOM_BYTES = 32 * 1024 * 1024
_FILE_NAME_LINE = re.compile(r"^FileName:\s*(?P<name>.+?)\s*$")
_LICENSE_LINE = re.compile(r"^LicenseConcluded:\s*(?P<license>.+?)\s*$")
_LICENSE_ID_LINE = re.compile(r"^LicenseID:\s*(?P<license>LicenseRef-[A-Za-z0-9.-]+)\s*$", re.I)
_EXTRACTED_TEXT_LINE = re.compile(r"^ExtractedText:\s*<text>", re.I)
_LICENSE_REFERENCE = re.compile(r"(?<![A-Za-z0-9.-])LicenseRef-[A-Za-z0-9.-]+", re.I)
_UNKNOWN_LICENSE_MARKERS = ("NOASSERTION", "NONE", "LICENSEREF-UNKNOWN")


def _file_license_conclusions(document: str) -> list[tuple[str, str | None]]:
    """Return each NCS SPDX file record and its concluded license, if present.

    NCS produces SPDX 2.2 tag-value records, where ``FileName`` starts a file
    record and ``LicenseConcluded`` is its license. Do not inspect the
    package-level ``PackageLicenseConcluded`` field: NCS intentionally emits
    ``NOASSERTION`` there even when all individual file licenses are known.
    """

    files: list[tuple[str, str | None]] = []
    current_name: str | None = None
    current_license: str | None = None

    for line in document.splitlines():
        file_match = _FILE_NAME_LINE.match(line)
        if file_match:
            if current_name is not None:
                files.append((current_name, current_license))
            current_name = file_match.group("name")
            current_license = None
            continue

        if current_name is None:
            continue
        license_match = _LICENSE_LINE.match(line)
        if license_match:
            if current_license is not None:
                raise SbomError(
                    "SPDX SBOM has multiple LicenseConcluded entries for "
                    f"file {current_name!r}"
                )
            current_license = license_match.group("license")

    if current_name is not None:
        files.append((current_name, current_license))
    return files


def _custom_license_definitions(document: str) -> dict[str, bool]:
    """Return custom SPDX IDs and whether each includes ExtractedText.

    SPDX 2.2 tag-value documents define custom licenses in the "Other Licensing
    Information Detected" section. The identifier and its text are required
    whenever a file concludes a LicenseRef expression.
    """

    definitions: dict[str, bool] = {}
    current_id: str | None = None
    for line in document.splitlines():
        license_id_match = _LICENSE_ID_LINE.match(line)
        if license_id_match:
            current_id = license_id_match.group("license").casefold()
            definitions[current_id] = False
            continue
        if current_id is not None and _EXTRACTED_TEXT_LINE.match(line):
            definitions[current_id] = True
    return definitions


def _custom_references(files: list[tuple[str, str | None]]) -> dict[str, str]:
    """Return each custom license reference used in a file conclusion."""

    references: dict[str, str] = {}
    for _, license_expression in files:
        if license_expression is None:
            continue
        for reference in _LICENSE_REFERENCE.findall(license_expression):
            references.setdefault(reference.casefold(), reference)
    return references


def validate_spdx(path: Path) -> None:
    """Require complete, non-placeholder license evidence per used file.

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
    files = _file_license_conclusions(document)
    if not files:
        raise SbomError("SPDX SBOM contains no FileName records")
    missing = [file_name for file_name, license_expression in files if license_expression is None]
    if missing:
        examples = ", ".join(missing[:4])
        raise SbomError(
            "SPDX SBOM has file records without LicenseConcluded entries; "
            f"refusing unattended publication: {examples}"
        )
    unknown = [
        f"{file_name}: {license_expression}"
        for file_name, license_expression in files
        if license_expression is not None
        and any(marker in license_expression.upper() for marker in _UNKNOWN_LICENSE_MARKERS)
    ]
    if unknown:
        examples = ", ".join(sorted(set(unknown))[:4])
        raise SbomError(
            "SPDX SBOM has unknown concluded file licenses; refusing unattended publication: "
            f"{examples}"
        )

    definitions = _custom_license_definitions(document)
    references = _custom_references(files)
    undefined = sorted(reference for key, reference in references.items() if key not in definitions)
    if undefined:
        raise SbomError(
            "SPDX SBOM has custom concluded licenses without LicenseID/ExtractedText "
            f"definitions; refusing unattended publication: {', '.join(undefined[:4])}"
        )
    missing_text = sorted(
        reference
        for key, reference in references.items()
        if not definitions[key]
    )
    if missing_text:
        raise SbomError(
            "SPDX SBOM has custom concluded licenses without ExtractedText; "
            f"refusing unattended publication: {', '.join(missing_text[:4])}"
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
