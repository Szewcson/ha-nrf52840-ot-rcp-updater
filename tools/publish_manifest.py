#!/usr/bin/env python3
"""Add one verified build metadata record to the published firmware manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path


class PublishError(RuntimeError):
    """The build metadata cannot safely become a published release entry."""


_VERSION = re.compile(
    r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"(?:-(?P<prerelease>preview|rc)(?P<sequence>[1-9][0-9]*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[A-Za-z0-9._+-]+\.elf$")


def _version_key(version: str) -> tuple[int, int, int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid NCS version: {version}")
    prerelease = match.group("prerelease")
    if prerelease is None:
        stage, sequence = 2, 0
    else:
        stage = 0 if prerelease == "preview" else 1
        sequence = int(match.group("sequence") or "0")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        stage,
        sequence,
    )


def _required_string(metadata: dict[str, object], name: str, pattern: re.Pattern[str]) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PublishError(f"metadata {name} is invalid")
    return value


def _required_u32(metadata: dict[str, object], name: str) -> int:
    value = metadata.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise PublishError(f"metadata {name} is invalid")
    return value


def _https_url(value: str, name: str) -> str:
    if not value.startswith("https://"):
        raise PublishError(f"{name} must use HTTPS")
    return value.rstrip("/")


def _write_manifest(manifest_path: Path, document: dict[str, object]) -> None:
    """Atomically persist normalized manifest JSON used by the runtime signer."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="manifest-", suffix=".json", dir=manifest_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, manifest_path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def migrate_manifest_signatures(manifest_path: Path, artifact_base_url: str) -> None:
    """Add deterministic detached-signature URLs before enforcing signatures.

    This one-time schema-compatible migration permits established firmware
    artifacts to receive their own signatures without changing their hashes.
    """

    artifact_base_url = _https_url(artifact_base_url, "artifact base URL")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"cannot read JSON input: {err}") from err
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise PublishError("manifest schema_version must be 1")
    releases = document.get("releases")
    if not isinstance(releases, list):
        raise PublishError("manifest releases must be an array")
    for release in releases:
        if not isinstance(release, dict) or not isinstance(release.get("artifact"), dict):
            raise PublishError("existing manifest release artifact is invalid")
        artifact = release["artifact"]
        assert isinstance(artifact, dict)
        filename = _required_string(artifact, "filename", _FILENAME)
        artifact["signature_url"] = f"{artifact_base_url}/{filename}.sig"
    _write_manifest(manifest_path, document)


def _validate_artifact(filename: str, digest: str, artifact: Path) -> None:
    """Bind publication metadata to exactly one locally verified ELF file."""

    if artifact.name != filename:
        raise PublishError("artifact filename does not match release metadata")
    if not artifact.is_file() or artifact.suffix != ".elf":
        raise PublishError("artifact file is missing or is not an ELF")
    try:
        actual_digest = sha256(artifact.read_bytes()).hexdigest()
    except OSError as err:
        raise PublishError(f"cannot hash artifact: {err}") from err
    if actual_digest != digest:
        raise PublishError("artifact SHA-256 does not match release metadata")


def update_manifest(
    manifest_path: Path,
    metadata_path: Path,
    artifact_path: Path,
    artifact_base_url: str,
    release_url: str,
) -> None:
    artifact_base_url = _https_url(artifact_base_url, "artifact base URL")
    release_url = _https_url(release_url, "release URL")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"cannot read JSON input: {err}") from err
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise PublishError("manifest schema_version must be 1")
    releases = document.get("releases")
    if not isinstance(releases, list):
        raise PublishError("manifest releases must be an array")
    if not isinstance(metadata, dict):
        raise PublishError("release metadata must be an object")

    hardware = _required_string(metadata, "hardware", re.compile(r"^[A-Za-z0-9._+-]+$"))
    ncs_version = _required_string(metadata, "ncs_version", _VERSION)
    zephyr_version = _required_string(metadata, "zephyr_version", _VERSION)
    dfu_application_version = _required_u32(metadata, "dfu_application_version")
    filename = _required_string(metadata, "artifact", _FILENAME)
    digest = _required_string(metadata, "sha256", _SHA256)
    _validate_artifact(filename, digest, artifact_path)
    entry = {
        "hardware": hardware,
        "ncs_version": ncs_version,
        "zephyr_version": zephyr_version,
        "dfu_application_version": dfu_application_version,
        "artifact": {
            "url": f"{artifact_base_url}/{filename}",
            "sha256": digest,
            "filename": filename,
            "signature_url": f"{artifact_base_url}/{filename}.sig",
        },
        "release_url": release_url,
        "release_summary": f"PCA10059 OpenThread RCP built with NCS {ncs_version} and Zephyr {zephyr_version}.",
    }
    remaining = [
        release
        for release in releases
        if not (
            isinstance(release, dict)
            and release.get("hardware") == hardware
            and release.get("ncs_version") == ncs_version
        )
    ]
    remaining.append(entry)
    try:
        remaining.sort(key=lambda release: _version_key(str(release["ncs_version"])))
    except (KeyError, ValueError) as err:
        raise PublishError("existing manifest has an invalid NCS version") from err
    document["releases"] = remaining

    _write_manifest(manifest_path, document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--artifact-base-url", required=True)
    parser.add_argument("--release-url")
    parser.add_argument("--migrate-signatures", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.migrate_signatures:
            if arguments.metadata is not None or arguments.artifact is not None:
                parser.error("signature migration does not accept --metadata or --artifact")
            migrate_manifest_signatures(arguments.manifest, arguments.artifact_base_url)
        else:
            if (
                arguments.metadata is None
                or arguments.artifact is None
                or arguments.release_url is None
            ):
                parser.error("publication requires --metadata, --artifact, and --release-url")
            update_manifest(
                arguments.manifest,
                arguments.metadata,
                arguments.artifact,
                arguments.artifact_base_url,
                arguments.release_url,
            )
    except PublishError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
