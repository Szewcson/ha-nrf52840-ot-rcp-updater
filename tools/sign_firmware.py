"""Create a portable detached Ed25519 signature for one firmware release file."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class SigningError(RuntimeError):
    """The protected firmware signing key could not sign the requested file."""


def sign(payload: bytes, private_key_pem: bytes) -> bytes:
    """Return the project detached-signature wire format for ``payload``."""

    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, ValueError) as err:
        raise SigningError("firmware signing key is not an unencrypted PEM private key") from err
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("firmware signing key must use Ed25519")
    return b"ed25519:" + base64.b64encode(key.sign(payload)) + b"\n"


def verify(payload: bytes, signature: bytes, public_key_pem: bytes) -> None:
    """Reject any signature that is not an Ed25519 signature for ``payload``."""

    try:
        encoded = signature.decode("ascii").strip()
    except UnicodeDecodeError as err:
        raise SigningError("firmware signature is not ASCII") from err
    if not encoded.startswith("ed25519:"):
        raise SigningError("firmware signature has an unsupported format")
    try:
        raw_signature = base64.b64decode(encoded.removeprefix("ed25519:"), validate=True)
    except (ValueError, binascii.Error) as err:
        raise SigningError("firmware signature is not valid base64") from err
    if len(raw_signature) != 64:
        raise SigningError("firmware signature has an invalid Ed25519 length")
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as err:
        raise SigningError("firmware verification key is not a PEM public key") from err
    if not isinstance(key, Ed25519PublicKey):
        raise SigningError("firmware verification key must use Ed25519")
    try:
        key.verify(raw_signature, payload)
    except InvalidSignature as err:
        raise SigningError("firmware signature does not match") from err


def sign_file(input_path: Path, private_key_path: Path, output_path: Path) -> None:
    """Atomically write a signature without exposing key material in arguments."""

    try:
        signature = sign(input_path.read_bytes(), private_key_path.read_bytes())
    except OSError as err:
        raise SigningError(f"cannot read signing input: {err}") from err
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="signature-", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(signature)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output_path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def verify_file(input_path: Path, signature_path: Path, public_key_path: Path) -> None:
    """Verify an existing release file before it is carried into a new manifest."""

    try:
        verify(input_path.read_bytes(), signature_path.read_bytes(), public_key_path.read_bytes())
    except OSError as err:
        raise SigningError(f"cannot read signature input: {err}") from err


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--private-key", type=Path)
    operation.add_argument("--public-key", type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--signature", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.private_key is not None:
            if arguments.output is None or arguments.signature is not None:
                parser.error("signing requires --output and does not accept --signature")
            sign_file(arguments.input, arguments.private_key, arguments.output)
        else:
            if arguments.signature is None or arguments.output is not None:
                parser.error("verification requires --signature and does not accept --output")
            assert arguments.public_key is not None
            verify_file(arguments.input, arguments.signature, arguments.public_key)
    except SigningError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
