"""Create an NCS-native SBOM cache from reviewed, evidence-backed license rules."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Mapping
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
_SOURCE_ROOT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DEBIAN_LICENSE_LABEL = re.compile(r"^[A-Za-z0-9.+-]{1,128}$")
_GLOB_METACHARACTERS = frozenset("*?[")
_SPDX_IDENTIFIER = b"SPDX-License-Identifier:"


@dataclass(frozen=True)
class Evidence:
    """A source file and exact text that supports one reviewed license rule."""

    path: PurePosixPath
    contains: tuple[str, ...]


@dataclass(frozen=True)
class PicolibcManifest:
    """Restricted mapping from installed Picolibc headers to its source manifest."""

    header_root: PurePosixPath
    source_prefix: PurePosixPath
    required_license: str
    generated_sources: tuple[tuple[PurePosixPath, PurePosixPath], ...]


@dataclass(frozen=True)
class DebianCopyrightStanza:
    """One ordered Files stanza from Picolibc's Debian copyright manifest."""

    patterns: tuple[re.Pattern[str], ...]
    license_label: str | None


@dataclass(frozen=True)
class Rule:
    """A restricted path glob mapped to one SPDX license expression."""

    source_root: str
    evidence_source_root: str
    path_glob: PurePosixPath
    license_expression: str
    evidence: Evidence
    picolibc_manifest: PicolibcManifest | None


def _relative_path(value: object, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SbomLicenseCacheError(f"{description} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SbomLicenseCacheError(f"{description} must stay inside its source root")
    return path


def _literal_relative_path(value: object, description: str) -> PurePosixPath:
    path = _relative_path(value, description)
    if any(character in path.as_posix() for character in _GLOB_METACHARACTERS):
        raise SbomLicenseCacheError(f"{description} must be an exact path, not a glob")
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


def _source_root_name(value: object, description: str) -> str:
    if not isinstance(value, str) or _SOURCE_ROOT_NAME.fullmatch(value) is None:
        raise SbomLicenseCacheError(f"{description} has an invalid source_root")
    return value


def _picolibc_manifest(
    value: object, evidence: Evidence, description: str
) -> PicolibcManifest | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SbomLicenseCacheError(f"{description} picolibc_manifest must be an object")
    required_fields = {
        "header_root",
        "source_prefix",
        "required_license",
        "generated_sources",
    }
    if set(value) != required_fields:
        raise SbomLicenseCacheError(
            f"{description} picolibc_manifest has unexpected or missing fields"
        )
    required_license = value["required_license"]
    if (
        not isinstance(required_license, str)
        or _DEBIAN_LICENSE_LABEL.fullmatch(required_license) is None
    ):
        raise SbomLicenseCacheError(f"{description} picolibc_manifest has an invalid license label")
    if not any(f"License: {required_license}" in text for text in evidence.contains):
        raise SbomLicenseCacheError(
            f"{description} evidence must explicitly verify {required_license!r}"
        )
    raw_generated_sources = value["generated_sources"]
    if not isinstance(raw_generated_sources, dict):
        raise SbomLicenseCacheError(
            f"{description} picolibc_manifest generated_sources must be an object"
        )
    generated_sources: list[tuple[PurePosixPath, PurePosixPath]] = []
    for header_path, manifest_path in raw_generated_sources.items():
        generated_sources.append(
            (
                _literal_relative_path(
                    header_path,
                    f"{description} picolibc_manifest generated header path",
                ),
                _literal_relative_path(
                    manifest_path,
                    f"{description} picolibc_manifest generated source path",
                ),
            )
        )
    return PicolibcManifest(
        header_root=_literal_relative_path(
            value["header_root"], f"{description} picolibc_manifest header_root"
        ),
        source_prefix=_literal_relative_path(
            value["source_prefix"], f"{description} picolibc_manifest source_prefix"
        ),
        required_license=required_license,
        generated_sources=tuple(sorted(generated_sources)),
    )


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
        required_fields = {"path_glob", "license", "reason", "evidence"}
        optional_fields = {"source_root", "evidence_source_root", "picolibc_manifest"}
        if not required_fields.issubset(raw_rule) or not set(raw_rule).issubset(
            required_fields | optional_fields
        ):
            raise SbomLicenseCacheError(f"{description} has unexpected or missing fields")
        source_root = _source_root_name(
            raw_rule.get("source_root", "workspace"), description
        )
        evidence_source_root = _source_root_name(
            raw_rule.get("evidence_source_root", source_root),
            f"{description} evidence_source_root",
        )
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
        evidence = Evidence(
            path=_relative_path(raw_evidence["path"], f"{description} evidence path"),
            contains=_required_text_list(
                raw_evidence["contains"], f"{description} evidence contains"
            ),
        )
        picolibc_manifest = _picolibc_manifest(
            raw_rule.get("picolibc_manifest"), evidence, description
        )
        if picolibc_manifest is not None and evidence_source_root != source_root:
            raise SbomLicenseCacheError(
                f"{description} Picolibc manifest evidence must use its source_root"
            )
        parsed.append(
            Rule(
                source_root=source_root,
                evidence_source_root=evidence_source_root,
                path_glob=path_glob,
                license_expression=license_expression,
                evidence=evidence,
                picolibc_manifest=picolibc_manifest,
            )
        )
    return tuple(parsed)


def _root_file(root: Path, relative_path: PurePosixPath, description: str) -> Path:
    path = root.joinpath(*relative_path.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as err:
        raise SbomLicenseCacheError(f"{description} escapes its source root") from err
    if not resolved.is_file():
        raise SbomLicenseCacheError(f"{description} is not a regular file")
    return resolved


def _root_directory(root: Path, relative_path: PurePosixPath, description: str) -> Path:
    path = root.joinpath(*relative_path.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as err:
        raise SbomLicenseCacheError(f"{description} escapes its source root") from err
    if not resolved.is_dir():
        raise SbomLicenseCacheError(f"{description} is not a directory")
    return resolved


def _verify_evidence(root: Path, evidence: Evidence) -> str:
    path = _root_file(root, evidence.path, "SBOM license evidence")
    try:
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise SbomLicenseCacheError(f"SBOM license evidence exceeds {_MAX_EVIDENCE_BYTES} bytes")
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise SbomLicenseCacheError(f"cannot read SBOM license evidence: {err}") from err
    missing = [text for text in evidence.contains if text not in contents]
    if missing:
        raise SbomLicenseCacheError(
            f"SBOM license evidence {evidence.path} no longer supports the policy rule; "
            f"missing required text {missing[0]!r}"
        )
    return contents


def _matching_files(root: Path, path_glob: PurePosixPath) -> tuple[Path, ...]:
    matches: list[Path] = []
    for path in sorted(root.glob(path_glob.as_posix())):
        if not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as err:
            raise SbomLicenseCacheError(f"SBOM license rule match escapes its source root: {path}") from err
        matches.append(path.resolve(strict=True))
    return tuple(matches)


def _debian_field(paragraph: str, field_name: str) -> tuple[str, ...] | None:
    """Return one Debian copyright field, including its continuation lines."""

    prefix = f"{field_name}:"
    lines = paragraph.splitlines()
    indexes = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(indexes) != 1:
        return None
    index = indexes[0]
    values = [lines[index].removeprefix(prefix).strip()]
    for line in lines[index + 1 :]:
        if not line.startswith((" ", "\t")):
            break
        values.append(line.strip())
    return tuple(values)


def _debian_pattern(pattern: str) -> re.Pattern[str]:
    """Compile the limited DEP-5 glob language without shell-specific extensions."""

    expression = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 1
            if index == len(pattern) or pattern[index] not in "*?\\":
                raise SbomLicenseCacheError(
                    f"Picolibc manifest has an invalid Files escape: {pattern!r}"
                )
            expression.append(re.escape(pattern[index]))
        elif character == "*":
            expression.append(".*")
        elif character == "?":
            expression.append(".")
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.compile("".join(expression))


def _debian_copyright_stanzas(contents: str) -> tuple[DebianCopyrightStanza, ...]:
    """Parse ordered DEP-5 Files stanzas so the manifest's last match wins."""

    stanzas: list[DebianCopyrightStanza] = []
    for paragraph in re.split(r"\n[ \t]*\n", contents.replace("\r\n", "\n")):
        files = _debian_field(paragraph, "Files")
        if not files:
            continue
        license_field = _debian_field(paragraph, "License")
        if not license_field:
            raise SbomLicenseCacheError("Picolibc manifest Files stanza lacks a License field")
        license_label = license_field[0]
        stanzas.append(
            DebianCopyrightStanza(
                patterns=tuple(_debian_pattern(pattern) for pattern in " ".join(files).split()),
                license_label=(
                    license_label
                    if _DEBIAN_LICENSE_LABEL.fullmatch(license_label) is not None
                    else None
                ),
            )
        )
    return tuple(stanzas)


def _debian_copyright_license(
    stanzas: tuple[DebianCopyrightStanza, ...], source_path: PurePosixPath
) -> str | None:
    """Return the final matching DEP-5 label, as required by the format specification."""

    license_label: str | None = None
    for stanza in stanzas:
        if any(pattern.fullmatch(source_path.as_posix()) for pattern in stanza.patterns):
            license_label = stanza.license_label
    return license_label


def _has_spdx_identifier(path: Path) -> bool:
    """Avoid overriding a header's own SPDX declaration with a fallback cache entry."""

    previous = b""
    try:
        if path.stat().st_size > _MAX_MATCHED_FILE_BYTES:
            raise SbomLicenseCacheError(
                f"SBOM license rule match exceeds {_MAX_MATCHED_FILE_BYTES} bytes: {path}"
            )
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                if _SPDX_IDENTIFIER in previous + chunk:
                    return True
                previous = chunk[-len(_SPDX_IDENTIFIER) + 1 :]
    except OSError as err:
        raise SbomLicenseCacheError(f"cannot read SBOM license rule match {path}: {err}") from err
    return False


def _picolibc_matches(
    root: Path, rule: Rule, matches: tuple[Path, ...], manifest_contents: str
) -> tuple[Path, ...]:
    """Keep only headers whose exact bundled Picolibc source record proves the policy label."""

    manifest = rule.picolibc_manifest
    if manifest is None:
        return matches
    header_root = _root_directory(root, manifest.header_root, "Picolibc header root")
    manifest_stanzas = _debian_copyright_stanzas(manifest_contents)
    generated_sources = dict(manifest.generated_sources)
    approved: list[Path] = []
    for path in matches:
        try:
            header_path = PurePosixPath(path.relative_to(header_root).as_posix())
        except ValueError as err:
            raise SbomLicenseCacheError(
                f"Picolibc header match is outside configured header root: {path}"
            ) from err
        if _has_spdx_identifier(path):
            continue
        source_path = generated_sources.get(
            header_path, manifest.source_prefix.joinpath(header_path)
        )
        if _debian_copyright_license(manifest_stanzas, source_path) == manifest.required_license:
            approved.append(path)
    return tuple(approved)


def _source_roots(
    workspace: Path, configured_roots: Mapping[str, Path] | None
) -> dict[str, Path]:
    """Resolve named, policy-approved external roots without widening globs."""

    roots = {"workspace": workspace}
    if configured_roots is None:
        return roots
    for name, path in configured_roots.items():
        name = _source_root_name(name, "configured SBOM source root")
        if name == "workspace":
            raise SbomLicenseCacheError("configured SBOM source root must not replace workspace")
        if name in roots:
            raise SbomLicenseCacheError(f"duplicate configured SBOM source root {name!r}")
        try:
            resolved = Path(path).resolve(strict=True)
        except OSError as err:
            raise SbomLicenseCacheError(f"cannot read SBOM source root {name!r}: {err}") from err
        if not resolved.is_dir():
            raise SbomLicenseCacheError(f"SBOM source root {name!r} is not a directory")
        roots[name] = resolved
    return roots


def _source_root_arguments(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not raw_path:
            raise SbomLicenseCacheError("--source-root must use NAME=PATH")
        if name in roots:
            raise SbomLicenseCacheError(f"duplicate configured SBOM source root {name!r}")
        roots[name] = Path(raw_path)
    return roots


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


def create_cache(
    workspace: Path,
    policy_path: Path,
    output_path: Path,
    source_roots: Mapping[str, Path] | None = None,
) -> int:
    """Write cache entries for current files that match reviewed policy rules."""

    try:
        workspace = workspace.resolve(strict=True)
    except OSError as err:
        raise SbomLicenseCacheError(f"cannot read NCS workspace: {err}") from err
    if not workspace.is_dir():
        raise SbomLicenseCacheError("NCS workspace is not a directory")

    roots = _source_roots(workspace, source_roots)
    files: dict[str, dict[str, object]] = {}
    for rule in _load_rules(policy_path):
        try:
            root = roots[rule.source_root]
        except KeyError as err:
            raise SbomLicenseCacheError(
                f"SBOM license policy source root {rule.source_root!r} is not configured"
            ) from err
        matches = _matching_files(root, rule.path_glob)
        if not matches:
            continue
        try:
            evidence_root = roots[rule.evidence_source_root]
        except KeyError as err:
            raise SbomLicenseCacheError(
                "SBOM license policy evidence_source_root "
                f"{rule.evidence_source_root!r} is not configured"
            ) from err
        evidence_contents = _verify_evidence(evidence_root, rule.evidence)
        matches = _picolibc_matches(root, rule, matches, evidence_contents)
        for path in matches:
            # NCS keys its cache against paths relative to the west workspace,
            # including reviewed external toolchain inputs as ../toolchains/...
            cache_path = Path(os.path.relpath(path, workspace)).as_posix()
            if cache_path in files:
                raise SbomLicenseCacheError(
                    f"multiple SBOM license rules match {cache_path}; policy must be unambiguous"
                )
            # NCS cache-database format uses SHA-1 as a content-match key.
            # Firmware authenticity remains protected independently by SHA-256 and Ed25519.
            files[cache_path] = {
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
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Reviewed external source root named by an SBOM policy rule",
    )
    arguments = parser.parse_args()
    try:
        count = create_cache(
            arguments.workspace,
            arguments.policy,
            arguments.output,
            _source_root_arguments(arguments.source_root),
        )
    except SbomLicenseCacheError as err:
        parser.error(str(err))
    print(f"Wrote NCS SBOM license cache with {count} reviewed file mapping(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
