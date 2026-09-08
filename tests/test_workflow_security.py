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
        self.assertIn("base64 --decode > \"${signing_key}\" 2>/dev/null", publish)
        self.assertIn("must be standard RFC 4648 Base64", publish)
        self.assertNotIn("base64 --ignore-garbage", publish)
        self.assertIn("tools/sign_firmware.py", publish)
        self.assertNotIn("find candidate", publish)
        self.assertNotIn("gh release", workflow)
        for evidence in (
            "NCS-LICENSE.txt",
            "PROJECT-NOTICE.txt",
            "sbom-license-policy.json",
            "sbom-license-cache.json",
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

        self.assertIn('"commoncode==32.3.0"', workflow)
        self.assertIn('"click==8.2.1"', workflow)
        self.assertIn("scancode-toolkit\\[full\\]==32\\.4\\.1", workflow)
        self.assertIn("--constraint \"${sbom_constraints}\"", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertIn(
            "--license-detectors spdx-tag,full-text,external-file,git-info,cache-database,scancode-toolkit",
            workflow,
        )
        self.assertIn("--input-cache-database", workflow)
        self.assertIn("tools/create_sbom_license_cache.py", workflow)
        self.assertIn('"build=${GITHUB_WORKSPACE}/candidate/build/coprocessor"', workflow)
        self.assertIn("0001-exclude-vcs-metadata.patch", workflow)
        self.assertIn("0002-exclude-derived-link-products.patch", workflow)
        self.assertIn(
            "def is_derived_link_product(self, path: Path) -> bool:",
            workflow,
        )
        self.assertIn('test -d "${repository}/.git"', workflow)
        self.assertIn("--optional-license-detectors cache-database,scancode-toolkit", workflow)
        self.assertIn(
            'west manifest --freeze --active-only > "${GITHUB_WORKSPACE}/candidate/west-manifest.yml"',
            workflow,
        )
        self.assertIn('--ncs-revision "$(git -C nrf rev-parse HEAD)"', workflow)
        self.assertNotIn('--ncs-revision "$(git rev-parse HEAD)"', workflow)
        self.assertNotIn("grep -Ev '^[[:space:]]*scancode-toolkit'", workflow)

    def test_derived_link_product_patch_is_narrow(self) -> None:
        patch = (
            Path(__file__).parents[1]
            / "patches"
            / "ncs-sbom"
            / "0002-exclude-derived-link-products.patch"
        ).read_text(encoding="utf-8")

        self.assertIn("def is_derived_link_product", patch)
        self.assertIn("path.suffix.lower() == '.elf'", patch)
        self.assertIn("path.suffix.lower() == '.cmd' and path.name.startswith('linker')", patch)
        self.assertIn("path.resolve().relative_to(self.build_dir.resolve())", patch)
        self.assertNotIn("SOURCE_CODE_SUFFIXES", patch)

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
