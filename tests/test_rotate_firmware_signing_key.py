from __future__ import annotations

import base64
import hashlib
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.rotate_firmware_signing_key import main
from tools.sign_firmware import sign, verify


class RotateFirmwareSigningKeyTests(unittest.TestCase):
    def test_generate_writes_private_material_outside_the_repository(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "rotation"

            self.assertEqual(
                main(["generate", "--output-dir", str(output), "--key-id", "replacement-20260908"]),
                0,
            )

            private_key = output / "replacement-20260908-private.pem"
            public_key = output / "replacement-20260908-public.pem"
            github_secret = output / "replacement-20260908-github-secret.b64"
            private_pem = private_key.read_bytes()
            public_pem = public_key.read_bytes()
            payload = b"rotation self-test"
            verify(payload, sign(payload, private_pem), public_pem)
            self.assertEqual(
                base64.b64decode(github_secret.read_bytes(), validate=True), private_pem
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(private_key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(github_secret.stat().st_mode), 0o600)

    def test_prepare_does_not_copy_the_existing_private_key(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "existing-private.pem"
            source_key = Ed25519PrivateKey.generate()
            source_pem = source_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            source.write_bytes(source_pem)
            source.chmod(0o600)
            output = root / "rotation"

            self.assertEqual(
                main(
                    [
                        "prepare",
                        "--private-key",
                        str(source),
                        "--output-dir",
                        str(output),
                        "--key-id",
                        "replacement-20260908",
                    ]
                ),
                0,
            )

            self.assertFalse((output / "replacement-20260908-private.pem").exists())
            public_pem = (output / "replacement-20260908-public.pem").read_bytes()
            payload = b"prepared key"
            verify(payload, sign(payload, source_pem), public_pem)

    def test_rejects_output_inside_the_repository(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(
                [
                    "generate",
                    "--output-dir",
                    str(Path(__file__).parents[1] / "unsafe-rotation-output"),
                    "--key-id",
                    "replacement-20260908",
                ]
            )

        self.assertEqual(raised.exception.code, 2)

    def test_refuses_to_overwrite_existing_key_material(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "rotation"
            output.mkdir(mode=0o700)
            existing = output / "replacement-20260908-public.pem"
            existing.write_bytes(b"do not overwrite")

            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "generate",
                        "--output-dir",
                        str(output),
                        "--key-id",
                        "replacement-20260908",
                    ]
                )

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(existing.read_bytes(), b"do not overwrite")
            self.assertFalse((output / "replacement-20260908-private.pem").exists())
            self.assertFalse((output / "replacement-20260908-github-secret.b64").exists())

    def test_refuses_the_current_key_before_writing_rotation_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "current-private.pem"
            replacement_private_key = Ed25519PrivateKey.generate()
            source.write_bytes(
                replacement_private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            source.chmod(0o600)
            output = root / "rotation"

            with patch(
                "tools.rotate_firmware_signing_key._trusted_fingerprints",
                return_value=frozenset(
                    {
                        hashlib.sha256(
                            replacement_private_key.public_key().public_bytes(
                                serialization.Encoding.DER,
                                serialization.PublicFormat.SubjectPublicKeyInfo,
                            )
                        ).hexdigest()
                    }
                ),
            ), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "prepare",
                        "--private-key",
                        str(source),
                        "--output-dir",
                        str(output),
                        "--key-id",
                        "replacement-20260908",
                    ]
                )

            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
