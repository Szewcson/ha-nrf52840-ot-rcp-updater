from __future__ import annotations

import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.manifest import (
    _FIRMWARE_SIGNING_LEGACY_PUBLIC_KEY_PEM,
    _FIRMWARE_SIGNING_PUBLIC_KEY_PEM,
    _verify_signature,
)

from tools.sign_firmware import SigningError, sign, verify, verify_any


def _keypair() -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _fingerprint(public_pem: bytes) -> str:
    public_key = serialization.load_pem_public_key(public_pem)
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return sha256(public_der).hexdigest()


class FirmwareSigningTests(unittest.TestCase):
    def test_signatures_verify_against_the_pinned_public_key_format(self) -> None:
        private_pem, public_pem = _keypair()
        payload = b"verified PCA10059 firmware"

        with patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", public_pem):
            _verify_signature(payload, sign(payload, private_pem), "test payload")
        verify(payload, sign(payload, private_pem), public_pem)

    def test_bridge_accepts_signatures_from_active_and_legacy_keys(self) -> None:
        active_private_pem, active_public_pem = _keypair()
        legacy_private_pem, legacy_public_pem = _keypair()
        payload = b"dual-trust bridge"

        with (
            patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", active_public_pem),
            patch("app.manifest._FIRMWARE_SIGNING_LEGACY_PUBLIC_KEY_PEM", legacy_public_pem),
        ):
            _verify_signature(payload, sign(payload, active_private_pem), "active signature")
            _verify_signature(payload, sign(payload, legacy_private_pem), "legacy signature")

    def test_signing_tool_accepts_any_complete_trusted_key_set(self) -> None:
        active_private_pem, active_public_pem = _keypair()
        legacy_private_pem, legacy_public_pem = _keypair()
        _, untrusted_public_pem = _keypair()
        payload = b"signing tool key set"

        verify_any(payload, sign(payload, active_private_pem), (active_public_pem, legacy_public_pem))
        verify_any(payload, sign(payload, legacy_private_pem), (active_public_pem, legacy_public_pem))
        with self.assertRaisesRegex(SigningError, "signature does not match"):
            verify_any(payload, sign(payload, active_private_pem), (untrusted_public_pem,))

    def test_pinned_bridge_key_fingerprints_are_intentional(self) -> None:
        self.assertEqual(
            _fingerprint(_FIRMWARE_SIGNING_PUBLIC_KEY_PEM),
            "03715e0d5084c77c230119639fc46f5e225ff6722cd53e171ec266ffff1b94ca",
        )
        self.assertEqual(
            _fingerprint(_FIRMWARE_SIGNING_LEGACY_PUBLIC_KEY_PEM),
            "6048da9611bedad11db1b43743a41abf59f64140f23eab979f45bd6da52f8aea",
        )


if __name__ == "__main__":
    unittest.main()
