from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.main import _load_options
from app.models import ValidationError


class MainOptionsTests(unittest.TestCase):
    def test_rejects_oversized_options_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_bytes(b"{" + b"x" * (64 * 1024))
            with (
                patch.dict(os.environ, {"OT_RCP_OPTIONS": str(path)}, clear=False),
                self.assertRaisesRegex(ValidationError, "exceed"),
            ):
                _load_options()


if __name__ == "__main__":
    unittest.main()
