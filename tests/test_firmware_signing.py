from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.manifest import _verify_signature

from tools.sign_firmware import sign, verify


class FirmwareSigningTests(unittest.TestCase):
    def test_signatures_verify_against_the_pinned_public_key_format(self) -> None:
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
        payload = b"verified PCA10059 firmware"

        with patch("app.manifest._FIRMWARE_SIGNING_PUBLIC_KEY_PEM", public_pem):
            _verify_signature(payload, sign(payload, private_pem), "test payload")
        verify(payload, sign(payload, private_pem), public_pem)


if __name__ == "__main__":
    unittest.main()
