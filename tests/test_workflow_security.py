from __future__ import annotations

import re
import unittest
from pathlib import Path


class WorkflowSecurityTests(unittest.TestCase):
    def test_release_workflow_uses_least_privilege_and_pinned_actions(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ncs-candidate.yml"
        ).read_text(encoding="utf-8")
        build = workflow[workflow.index("  build:") : workflow.index("  publish:")]
        publish = workflow[workflow.index("  publish:") :]

        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("permissions:\n      contents: write", publish)
        self.assertNotIn("GITHUB_TOKEN", build)
        self.assertNotIn("personal-access-token", build)
        for action in (
            "actions/checkout",
            "actions/upload-artifact",
            "actions/download-artifact",
        ):
            self.assertRegex(workflow, rf"uses: {re.escape(action)}@[0-9a-f]{{40}}(?:\s|$)")


if __name__ == "__main__":
    unittest.main()
