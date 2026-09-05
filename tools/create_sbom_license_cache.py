"""Create an NCS-native SBOM cache from reviewed, evidence-backed license rules."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path, PurePosixPath


class SbomLicenseCacheError(RuntimeError):
    """The reviewed SBOM license policy cannot safely produce a cache."""


_MAX_POLICY_BYTES = 64 * 1024
_MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
_MAX_MATCHED_FILE_BYTES = 64 * 1024 * 1024
_MAX_LICENSE_LENGTH = 256
_HASH_CHUNK_BYTES = 64 * 1024
_LICENSE_EXPRESSION = re.compile(r"^[A-Za-z0-9 .()+-]+$")
_PLACEHOLDER_LICENSES = frozenset({"NOASSERTION", "NONE", "LICENSEREF-UNKNOWN"})


@dataclass(frozen=True)
class Evidence:
    """A source file and exact text that supports one reviewed license rule."""

    path: PurePosixPath
    contains: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    """A restricted path glob mapped to one SPDX license expression."""

    path_glob: PurePosixPath
    license_expression: str
    evidence: Evidence


def _relative_path(value: object, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SbomLicenseCacheError(f"{description} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SbomLicenseCacheError(f"{description} must stay inside the NCS workspace")
    return path


def _required_text_list(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SbomLicenseCacheError(f"{description} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise SbomLicenseCacheError(f"{description} must contain non-empty strings")
        result.append(item)
    return tuple(result)


def _load_rules(policy_path: Path) -> tuple[Rule, ...]:
    try:
        if policy_path.stat().st_size > _MAX_POLICY_BYTES:
            raise SbomLicenseCacheError(f"SBOM license policy exceeds {_MAX_POLICY_BYTES} bytes")
        document = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SbomLicenseCacheError(f"cannot read SBOM license policy: {err}") from err
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise SbomLicenseCacheError("SBOM license policy schema_version must be 1")
    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        raise SbomLicenseCacheError("SBOM license policy rules must be a non-empty list")

    parsed: list[Rule] = []
    for index, raw_rule in enumerate(rules):
        description = f"SBOM license policy rule {index}"
        if not isinstance(raw_rule, dict):
            raise SbomLicenseCacheError(f"{description} must be an object")
        if set(raw_rule) != {"path_glob", "license", "reason", "evidence"}:
            raise SbomLicenseCacheError(f"{description} has unexpected or missing fields")
        path_glob = _relative_path(raw_rule["path_glob"], f"{description} path_glob")
        license_expression = raw_rule["license"]
        if (
            not isinstance(license_expression, str)
            or not license_expression
            or len(license_expression) > _MAX_LICENSE_LENGTH
            or _LICENSE_EXPRESSION.fullmatch(license_expression) is None
            or license_expression.upper() in _PLACEHOLDER_LICENSES
        ):
            raise SbomLicenseCacheError(f"{description} has an invalid license expression")
        if not isinstance(raw_rule["reason"], str) or not raw_rule["reason"].strip():
            raise SbomLicenseCacheError(f"{description} needs a non-empty reason")
        raw_evidence = raw_rule["evidence"]
        if not isinstance(raw_evidence, dict) or set(raw_evidence) != {"path", "contains"}:
            raise SbomLicenseCacheError(f"{description} evidence has unexpected or missing fields")
        parsed.append(
            Rule(
                path_glob=path_glob,
                license_expression=license_expression,
                evidence=Evidence(
                    path=_relative_path(raw_evidence["path"], f"{description} evidence path"),
                    contains=_required_text_list(
                        raw_evidence["contains"], f"{description} evidence contains"
                    ),
                ),
            )
        )
    return tuple(parsed)


def _workspace_file(workspace: Path, relative_path: PurePosixPath, description: str) -> Path:
    path = workspace.joinpath(*relative_path.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(workspace)
    except (OSError, ValueError) as err:
        raise SbomLicenseCacheError(f"{description} escapes the NCS workspace") from err
    if not resolved.is_file():
        raise SbomLicenseCacheError(f"{description} is not a regular file")
    return resolved


def _verify_evidence(workspace: Path, evidence: Evidence) -> None:
    path = _workspace_file(workspace, evidence.path, "SBOM license evidence")
    try:
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise SbomLicenseCacheError(f"SBOM license evidence exceeds {_MAX_EVIDENCE_BYTES} bytes")
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise SbomLicenseCacheError(f"cannot read SBOM license evidence: {err}") from err
    missing = [text for text in evidence.contains if text not in contents]
    if missing:
        raise SbomLicenseCacheError(
            f"SBOM license evidence {evidence.path} no longer supports the policy rule"
        )


def _matching_files(workspace: Path, path_glob: PurePosixPath) -> tuple[Path, ...]:
    matches: list[Path] = []
    for path in sorted(workspace.glob(path_glob.as_posix())):
        if not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError) as err:
            raise SbomLicenseCacheError(f"SBOM license rule match escapes the NCS workspace: {path}") from err
        matches.append(path.resolve(strict=True))
    return tuple(matches)


def _sha1_file(path: Path) -> str:
    """Return NCS's required cache digest without unbounded file buffering."""

    try:
        if path.stat().st_size > _MAX_MATCHED_FILE_BYTES:
            raise SbomLicenseCacheError(
                f"SBOM license rule match exceeds {_MAX_MATCHED_FILE_BYTES} bytes: {path}"
            )
        digest = sha1()
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as err:
        raise SbomLicenseCacheError(f"cannot hash SBOM license rule match {path}: {err}") from err
    return digest.hexdigest()


def _write_cache(output_path: Path, document: dict[str, object]) -> None:
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="sbom-license-cache-", suffix=".json", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, output_path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def create_cache(workspace: Path, policy_path: Path, output_path: Path) -> int:
    """Write cache entries for current files that match reviewed policy rules."""

    try:
        workspace = workspace.resolve(strict=True)
    except OSError as err:
        raise SbomLicenseCacheError(f"cannot read NCS workspace: {err}") from err
    if not workspace.is_dir():
        raise SbomLicenseCacheError("NCS workspace is not a directory")

    files: dict[str, dict[str, object]] = {}
    for rule in _load_rules(policy_path):
        matches = _matching_files(workspace, rule.path_glob)
        if not matches:
            continue
        _verify_evidence(workspace, rule.evidence)
        for path in matches:
            relative_path = path.relative_to(workspace).as_posix()
            if relative_path in files:
                raise SbomLicenseCacheError(
                    f"multiple SBOM license rules match {relative_path}; policy must be unambiguous"
                )
            # NCS cache-database format uses SHA-1 as a content-match key.
            # Firmware authenticity remains protected independently by SHA-256 and Ed25519.
            files[relative_path] = {
                "sha1": _sha1_file(path),
                "license": [rule.license_expression],
            }
    _write_cache(output_path, {"files": files})
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        count = create_cache(arguments.workspace, arguments.policy, arguments.output)
    except SbomLicenseCacheError as err:
        parser.error(str(err))
    print(f"Wrote NCS SBOM license cache with {count} reviewed file mapping(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
