"""Write immutable build provenance next to an NCS firmware artifact."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path


class ProvenanceError(RuntimeError):
    """Inputs cannot safely describe one published firmware artifact."""


_MAX_METADATA_BYTES = 64 * 1024
_MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _read(path: Path, limit: int, description: str) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise ProvenanceError(f"{description} exceeds {limit} bytes")
        return path.read_bytes()
    except OSError as err:
        raise ProvenanceError(f"cannot read {description}: {err}") from err


def _digest(path: Path, limit: int, description: str) -> str:
    return sha256(_read(path, limit, description)).hexdigest()


def _metadata(path: Path) -> dict[str, object]:
    try:
        document = json.loads(_read(path, _MAX_METADATA_BYTES, "release metadata").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ProvenanceError("release metadata is not valid UTF-8 JSON") from err
    if not isinstance(document, dict):
        raise ProvenanceError("release metadata is not an object")
    for key in ("artifact", "ncs_version", "sha256", "zephyr_version"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise ProvenanceError(f"release metadata {key} is missing")
    return document


def create_provenance(
    metadata_path: Path,
    artifact_path: Path,
    ncs_tag: str,
    ncs_revision: str,
    ncs_license_path: Path,
    west_manifest_path: Path,
    toolchain_report_path: Path,
    source_revision: str,
    output_path: Path,
) -> None:
    """Bind the produced ELF to its resolved sources, toolchain, and license."""

    metadata = _metadata(metadata_path)
    artifact_name = metadata["artifact"]
    artifact_sha256 = metadata["sha256"]
    assert isinstance(artifact_name, str)
    assert isinstance(artifact_sha256, str)
    if artifact_path.name != artifact_name:
        raise ProvenanceError("artifact name does not match release metadata")
    if _digest(artifact_path, _MAX_EVIDENCE_BYTES * 16, "firmware ELF") != artifact_sha256:
        raise ProvenanceError("artifact SHA-256 does not match release metadata")
    if not ncs_tag.startswith("v") or not _GIT_REVISION.fullmatch(ncs_revision):
        raise ProvenanceError("NCS tag and full source revision are required")
    if not _GIT_REVISION.fullmatch(source_revision):
        raise ProvenanceError("full project source revision is required")

    document = {
        "schema_version": 1,
        "artifact": {
            "filename": artifact_name,
            "sha256": artifact_sha256,
        },
        "build_metadata": metadata,
        "ncs": {
            "tag": ncs_tag,
            "revision": ncs_revision,
            "license_sha256": _digest(ncs_license_path, _MAX_EVIDENCE_BYTES, "NCS license"),
            "resolved_west_manifest_sha256": _digest(
                west_manifest_path, _MAX_EVIDENCE_BYTES, "resolved west manifest"
            ),
        },
        "source_repository_revision": source_revision,
        "toolchain_report_sha256": _digest(
            toolchain_report_path, _MAX_EVIDENCE_BYTES, "toolchain report"
        ),
    }
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="provenance-", suffix=".json", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output_path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--ncs-tag", required=True)
    parser.add_argument("--ncs-revision", required=True)
    parser.add_argument("--ncs-license", required=True, type=Path)
    parser.add_argument("--west-manifest", required=True, type=Path)
    parser.add_argument("--toolchain-report", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        create_provenance(
            arguments.metadata,
            arguments.artifact,
            arguments.ncs_tag,
            arguments.ncs_revision,
            arguments.ncs_license,
            arguments.west_manifest,
            arguments.toolchain_report,
            arguments.source_revision,
            arguments.output,
        )
    except ProvenanceError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
