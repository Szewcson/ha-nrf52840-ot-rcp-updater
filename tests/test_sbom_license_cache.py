from __future__ import annotations

import json
import sys
import unittest
from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.create_sbom_license_cache import SbomLicenseCacheError, create_cache


def _policy(rules: list[dict[str, object]]) -> str:
    return json.dumps({"schema_version": 1, "rules": rules})


def _rule(path_glob: str, license_expression: str, evidence_path: str) -> dict[str, object]:
    return {
        "path_glob": path_glob,
        "license": license_expression,
        "reason": "Test evidence-backed mapping.",
        "evidence": {
            "path": evidence_path,
            "contains": ["SPDX-License-Identifier: Apache-2.0"],
        },
    }


class SbomLicenseCacheTests(unittest.TestCase):
    def test_production_policy_maps_only_named_zephyr_files(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        document = json.loads(policy_path.read_text(encoding="utf-8"))
        zephyr_rules = [
            rule for rule in document["rules"] if rule["path_glob"].startswith("zephyr/")
        ]

        self.assertEqual(
            {rule["path_glob"] for rule in zephyr_rules},
            {
                "zephyr/VERSION",
                "zephyr/include/zephyr/linker/ram-end.ld",
                "zephyr/misc/empty_file.c",
                "zephyr/subsys/usb/device_next/usbd_data.ld",
            },
        )
        self.assertTrue(all(rule["license"] == "Apache-2.0" for rule in zephyr_rules))
        self.assertTrue(
            all(rule["evidence"]["path"] == "zephyr/LICENSE" for rule in zephyr_rules)
        )

    def test_creates_hash_bound_cache_for_reviewed_matches(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "component" / "LICENSE"
            evidence.parent.mkdir()
            evidence.write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            source = root / "component" / "generated.h"
            source.write_bytes(b"generated source")
            (root / "unrelated.c").write_text("no mapping\n", encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(
                _policy([_rule("component/*.h", "Apache-2.0", "component/LICENSE")]),
                encoding="utf-8",
            )
            output = root / "cache.json"

            self.assertEqual(create_cache(root, policy, output), 1)

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "files": {
                    "component/generated.h": {
                        "license": ["Apache-2.0"],
                        "sha1": sha1(b"generated source").hexdigest(),
                    }
                }
            },
        )

    def test_rejects_missing_evidence_for_a_matching_rule(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "component" / "generated.h"
            source.parent.mkdir()
            source.write_text("generated\n", encoding="utf-8")
            (root / "component" / "LICENSE").write_text("different license\n", encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(
                _policy([_rule("component/*.h", "Apache-2.0", "component/LICENSE")]),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SbomLicenseCacheError, "missing required text 'SPDX-License-Identifier: Apache-2.0'"
            ):
                create_cache(root, policy, root / "cache.json")

    def test_rejects_ambiguous_matching_rules(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "component" / "LICENSE"
            evidence.parent.mkdir()
            evidence.write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            (root / "component" / "generated.h").write_text("generated\n", encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule("component/*.h", "Apache-2.0", "component/LICENSE"),
                        _rule("component/generated.h", "Apache-2.0", "component/LICENSE"),
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SbomLicenseCacheError, "multiple SBOM license rules"):
                create_cache(root, policy, root / "cache.json")

    def test_rejects_placeholder_license_regardless_of_case(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "component" / "LICENSE"
            evidence.parent.mkdir()
            evidence.write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            (root / "component" / "generated.h").write_text("generated\n", encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(
                _policy([_rule("component/*.h", "noassertion", "component/LICENSE")]),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SbomLicenseCacheError, "invalid license expression"):
                create_cache(root, policy, root / "cache.json")


if __name__ == "__main__":
    unittest.main()
