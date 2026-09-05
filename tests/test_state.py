from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.state import StateError, StateStore


class StateStoreTests(unittest.TestCase):
    def test_rejects_an_oversized_persisted_state_before_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b"{" + b"x" * (64 * 1024))

            with self.assertRaisesRegex(StateError, "exceeds"):
                StateStore(Path(directory)).load()


if __name__ == "__main__":
    unittest.main()
