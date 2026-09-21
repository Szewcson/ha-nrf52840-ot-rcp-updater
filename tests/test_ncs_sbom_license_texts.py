from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.install_ncs_sbom_license_texts import NcsSbomLicenseTextError, install_license_texts


class NcsSbomLicenseTextsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.additions = Path(__file__).parents[1] / "firmware" / "sbom-license-texts.yaml"

    def test_installs_reviewed_texts_once(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "license-texts.yaml"
            database.write_text(
                "- id: LicenseRef-Nordic-5-Clause\n  text: |\n    Nordic text\n",
                encoding="utf-8",
            )

            self.assertTrue(install_license_texts(database, self.additions))
            installed = database.read_text(encoding="utf-8")
            self.assertIn("LicenseRef-scancode-red-hat-attribution", installed)
            self.assertIn("LicenseRef-scancode-delorie-historical", installed)
            self.assertFalse(install_license_texts(database, self.additions))

    def test_rejects_partially_upstreamed_license_set(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "license-texts.yaml"
            database.write_text(
                "- id: LicenseRef-scancode-red-hat-attribution\n  text: |\n    Existing text\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(NcsSbomLicenseTextError, "only part"):
                install_license_texts(database, self.additions)


if __name__ == "__main__":
    unittest.main()
