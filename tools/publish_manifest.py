#!/usr/bin/env python3
"""Add one verified build metadata record to the published firmware manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile


class PublishError(RuntimeError):
    """The build metadata cannot safely become a published release entry."""


_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[A-Za-z0-9._+-]+\.zip$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG = re.compile(r"^[A-Za-z0-9._-]+$")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _required_string(metadata: dict[str, object], name: str, pattern: re.Pattern[str]) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PublishError(f"metadata {name} is invalid")
    return value


def update_manifest(manifest_path: Path, metadata_path: Path, repository: str, release_tag: str) -> None:
    if not _REPOSITORY.fullmatch(repository):
        raise PublishError("repository must have OWNER/REPOSITORY form")
    if not _TAG.fullmatch(release_tag):
        raise PublishError("release tag contains unsupported characters")
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
    filename = _required_string(metadata, "artifact", _FILENAME)
    digest = _required_string(metadata, "sha256", _SHA256)
    entry = {
        "hardware": hardware,
        "ncs_version": ncs_version,
        "zephyr_version": zephyr_version,
        "artifact": {
            "url": f"https://github.com/{repository}/releases/download/{release_tag}/{filename}",
            "sha256": digest,
            "filename": filename,
        },
        "release_url": f"https://github.com/{repository}/releases/tag/{release_tag}",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    arguments = parser.parse_args()
    try:
        update_manifest(
            arguments.manifest,
            arguments.metadata,
            arguments.repository,
            arguments.release_tag,
        )
    except PublishError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
