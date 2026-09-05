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

    def test_publication_requires_signed_artifacts_and_release_evidence(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ncs-candidate.yml"
        ).read_text(encoding="utf-8")
        publish = workflow[workflow.index("  publish:") :]

        self.assertIn("environment: firmware-publisher", publish)
        self.assertIn("FIRMWARE_SIGNING_PRIVATE_KEY_B64", publish)
        self.assertIn("tools/sign_firmware.py", publish)
        self.assertNotIn("find candidate", publish)
        self.assertNotIn("gh release", workflow)
        for evidence in (
            "NCS-LICENSE.txt",
            "PROJECT-NOTICE.txt",
            "firmware.spdx",
            "firmware-notices.html",
            "provenance.json",
            "west-manifest.yml",
            "zephyr-sdk.txt",
        ):
            self.assertIn(evidence, workflow)

    def test_sbom_uses_deterministic_ncs_detectors(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ncs-candidate.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("grep -Ev '^[[:space:]]*scancode-toolkit'", workflow)
        self.assertIn(
            "--license-detectors spdx-tag,full-text,external-file,git-info", workflow
        )
        self.assertIn('--optional-license-detectors ""', workflow)

    def test_pull_request_ci_reuses_the_full_verification_baseline(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("pull_request:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertRegex(workflow, r"uses: actions/checkout@[0-9a-f]{40}(?:\s|$)")
        for command in (
            "ruff check .",
            "unittest discover",
            "apparmor_parser",
            "docker build",
        ):
            self.assertIn(command, workflow)


if __name__ == "__main__":
    unittest.main()
