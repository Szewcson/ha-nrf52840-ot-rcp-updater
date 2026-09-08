"""Prepare a replacement Ed25519 firmware-signing key outside this checkout.

The helper creates only local key material. Rotating the public verifier still
requires a bridge add-on release before the GitHub environment secret changes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

if __package__:
    from .sign_firmware import SigningError, sign, verify
else:
    from sign_firmware import SigningError, sign, verify


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TRUSTED_PUBLIC_KEY_PATHS = (
    _PROJECT_ROOT / "nrf52840_ot_rcp_updater/app/firmware_signing_public_key.pem",
    _PROJECT_ROOT / "nrf52840_ot_rcp_updater/app/firmware_signing_legacy_public_key.pem",
)
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RotationError(RuntimeError):
    """The replacement key material cannot be safely prepared."""


@dataclass(frozen=True)
class KeyMaterial:
    """Validated private/public material for one Ed25519 signing identity."""

    private_pem: bytes
    public_pem: bytes
    public_fingerprint: str


@dataclass(frozen=True)
class RotationFiles:
    """Paths written by one successful key-material preparation."""

    private_key: Path | None
    public_key: Path
    github_secret: Path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _outside_project(path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, _PROJECT_ROOT):
        raise RotationError(
            f"{description} must be outside the repository so private key material cannot be committed"
        )
    return resolved


def _validate_key_id(key_id: str) -> str:
    if not _KEY_ID_PATTERN.fullmatch(key_id):
        raise RotationError(
            "key ID must contain 1-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return key_id


def _private_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _material_from_private_pem(private_pem: bytes) -> KeyMaterial:
    try:
        private_key = serialization.load_pem_private_key(private_pem, password=None)
    except (TypeError, ValueError) as err:
        raise RotationError("private key must be an unencrypted PEM private key") from err
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RotationError("private key must use Ed25519")

    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    challenge = secrets.token_bytes(32)
    try:
        verify(challenge, sign(challenge, private_pem), public_pem)
    except SigningError as err:
        raise RotationError("replacement key failed the project's signing self-test") from err
    return KeyMaterial(private_pem, public_pem, hashlib.sha256(public_der).hexdigest())


def generate_key_material() -> KeyMaterial:
    """Generate and self-test a new Ed25519 signing keypair."""

    return _material_from_private_pem(_private_key_pem(Ed25519PrivateKey.generate()))


def _read_private_key(path: Path) -> bytes:
    resolved = _outside_project(path, "private key")
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError as err:
        raise RotationError(f"cannot inspect private key: {err}") from err
    if mode & 0o077:
        raise RotationError(
            f"private key {resolved} is accessible to group or other users; set its mode to 0600"
        )
    try:
        return resolved.read_bytes()
    except OSError as err:
        raise RotationError(f"cannot read private key: {err}") from err


def _trusted_fingerprints() -> frozenset[str]:
    """Return every verifier that a future replacement key must not duplicate."""

    fingerprints: set[str] = set()
    for index, public_key_path in enumerate(_TRUSTED_PUBLIC_KEY_PATHS):
        if not public_key_path.exists():
            if index == 0:
                raise RotationError("cannot read the repository's active public signing key")
            continue
        try:
            public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        except (OSError, TypeError, ValueError) as err:
            raise RotationError("cannot read a repository firmware signing public key") from err
        public_der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        fingerprints.add(hashlib.sha256(public_der).hexdigest())
    return frozenset(fingerprints)


def _prepare_output_directory(path: Path) -> Path:
    directory = _outside_project(path, "output directory")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = stat.S_IMODE(directory.stat().st_mode)
    except OSError as err:
        raise RotationError(f"cannot prepare output directory {directory}: {err}") from err
    if not directory.is_dir():
        raise RotationError(f"output directory {directory} is not a directory")
    if mode & 0o077:
        raise RotationError(
            f"output directory {directory} is accessible to group or other users; use a dedicated mode-0700 directory"
        )
    return directory


def _write_new_file(path: Path, contents: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as err:
        raise RotationError(f"refusing to overwrite existing key material: {path}") from err
    except OSError as err:
        raise RotationError(f"cannot create {path}: {err}") from err
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as err:
        try:
            path.unlink()
        except OSError:
            pass
        raise RotationError(f"cannot write {path}: {err}") from err


def write_rotation_files(
    material: KeyMaterial,
    output_directory: Path,
    key_id: str,
    *,
    include_private_key: bool,
) -> RotationFiles:
    """Write mode-0600 rotation files without overwriting prior material."""

    validated_key_id = _validate_key_id(key_id)
    directory = _prepare_output_directory(output_directory)
    private_key = directory / f"{validated_key_id}-private.pem"
    public_key = directory / f"{validated_key_id}-public.pem"
    github_secret = directory / f"{validated_key_id}-github-secret.b64"
    targets = [public_key, github_secret]
    if include_private_key:
        targets.insert(0, private_key)
    for target in targets:
        if target.exists() or target.is_symlink():
            raise RotationError(f"refusing to overwrite existing key material: {target}")

    written: list[Path] = []
    try:
        if include_private_key:
            _write_new_file(private_key, material.private_pem)
            written.append(private_key)
        _write_new_file(public_key, material.public_pem)
        written.append(public_key)
        _write_new_file(github_secret, base64.b64encode(material.private_pem))
        written.append(github_secret)
    except RotationError:
        for path in reversed(written):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return RotationFiles(private_key if include_private_key else None, public_key, github_secret)


def _default_key_id() -> str:
    return f"firmware-signing-{date.today():%Y%m%d}-{secrets.token_hex(3)}"


def _report(files: RotationFiles, material: KeyMaterial) -> None:
    print("Replacement firmware signing key prepared and self-tested.")
    if files.private_key is not None:
        print(f"KeePassXC backup private-key path (mode 0600): {files.private_key}")
    print(f"Public key: {files.public_key}")
    print(f"GitHub secret value (mode 0600): {files.github_secret}")
    print(f"Public-key DER SHA-256 fingerprint: {material.public_fingerprint}")
    print()
    print("Do not paste or commit the private key or GitHub-secret file.")
    print("Back up the private PEM in KeePassXC, then provide only the public PEM and fingerprint")
    print("to prepare the bridge add-on release. Set the GitHub environment secret only after")
    print("that bridge release is installed.")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="dedicated mode-0700 directory outside the repository",
    )
    parser.add_argument(
        "--key-id",
        default=None,
        help="safe filename prefix; default includes the current date and random suffix",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rotate_firmware_signing_key.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="generate a new Ed25519 keypair")
    _add_common_arguments(generate)
    prepare = commands.add_parser(
        "prepare", help="validate an existing Ed25519 private PEM and derive rotation files"
    )
    _add_common_arguments(prepare)
    prepare.add_argument("--private-key", required=True, type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(arguments)
    key_id = parsed.key_id or _default_key_id()
    try:
        if parsed.command == "generate":
            material = generate_key_material()
            include_private_key = True
        else:
            material = _material_from_private_pem(_read_private_key(parsed.private_key))
            include_private_key = False
        if material.public_fingerprint in _trusted_fingerprints():
            raise RotationError(
                "candidate key matches an already trusted public key; this is not a rotation"
            )
        files = write_rotation_files(
            material, parsed.output_dir, key_id, include_private_key=include_private_key
        )
    except RotationError as err:
        parser.error(str(err))
    _report(files, material)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
