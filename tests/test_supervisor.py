from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.supervisor import SupervisorClient, SupervisorError


class SupervisorRestoreTests(unittest.TestCase):
    def test_preserves_operation_error_when_otbr_restore_fails(self) -> None:
        client = object.__new__(SupervisorClient)
        states = iter(["running", "stopped", "stopped", "stopped"])
        client.addon_state = lambda slug: next(states, "stopped")
        client.stop_addon = lambda slug: None
        client.wait_for_state = lambda slug, expected, timeout=30: None
        client.start_addon = lambda slug: (_ for _ in ()).throw(SupervisorError("HTTP 400"))

        with (
            patch("app.supervisor.sleep"),
            self.assertRaisesRegex(RuntimeError, "DFU target missing") as captured,
            client.temporarily_stop("core_openthread_border_router"),
        ):
            raise RuntimeError("DFU target missing")

        self.assertEqual(len(captured.exception.__notes__), 1)
        self.assertIn("OTBR could not be restarted", captured.exception.__notes__[0])


if __name__ == "__main__":
    unittest.main()
