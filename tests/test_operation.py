from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.operation import FlashCommand, OperationBusyError, OperationController


class OperationControllerTests(unittest.TestCase):
    def test_concurrent_submitters_reserve_exactly_one_hardware_operation(self) -> None:
        controller = OperationController()
        barrier = threading.Barrier(9)
        accepted: list[str] = []
        rejected: list[OperationBusyError] = []
        result_lock = threading.Lock()

        def submit() -> None:
            barrier.wait()
            try:
                operation_id = controller.submit(FlashCommand.install_latest())
            except OperationBusyError as err:
                with result_lock:
                    rejected.append(err)
            else:
                with result_lock:
                    accepted.append(operation_id)

        workers = [threading.Thread(target=submit) for _ in range(8)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 7)
        operation = controller.next(0)
        self.assertIsNotNone(operation)
        assert operation is not None
        controller.start(operation)
        controller.report_progress(operation, 70, "Flashing firmware")
        controller.complete(operation)
        snapshot = controller.operation_snapshot()
        self.assertFalse(snapshot["busy"])
        self.assertEqual(snapshot["state"], "complete")
        self.assertEqual(snapshot["progress"], 100)
        self.assertIn("Flashing firmware", [event["message"] for event in snapshot["events"]])

    def test_runtime_snapshots_do_not_expose_mutable_shared_state(self) -> None:
        controller = OperationController()
        installed = {"ncs_version": "3.4.0"}
        diagnostics = {"configured_rcp_device_present": True}
        controller.update_runtime(
            installed=installed,
            release=None,
            releases=(),
            manifest_error=None,
            diagnostics=diagnostics,
        )
        installed["ncs_version"] = "tampered"
        diagnostics["configured_rcp_device_present"] = False

        snapshot = controller.runtime_snapshot()
        self.assertEqual(snapshot.installed["ncs_version"], "3.4.0")
        self.assertTrue(snapshot.diagnostics["configured_rcp_device_present"])
        snapshot.installed["ncs_version"] = "local copy"
        self.assertEqual(controller.runtime_snapshot().installed["ncs_version"], "3.4.0")


if __name__ == "__main__":
    unittest.main()
