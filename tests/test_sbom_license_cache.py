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


def _rule(
    path_glob: str,
    license_expression: str,
    evidence_path: str,
    *,
    source_root: str | None = None,
    evidence_source_root: str | None = None,
    evidence_contains: list[str] | None = None,
    picolibc_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "path_glob": path_glob,
        "license": license_expression,
        "reason": "Test evidence-backed mapping.",
        "evidence": {
            "path": evidence_path,
            "contains": evidence_contains or ["SPDX-License-Identifier: Apache-2.0"],
        },
    }
    if source_root is not None:
        rule["source_root"] = source_root
    if evidence_source_root is not None:
        rule["evidence_source_root"] = evidence_source_root
    if picolibc_manifest is not None:
        rule["picolibc_manifest"] = picolibc_manifest
    return rule


class SbomLicenseCacheTests(unittest.TestCase):
    def test_production_policy_accepts_mbedtls_markdown_dual_license_banner(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "modules" / "crypto" / "mbedtls" / "library" / "mbedtls_config_check_before.h"
            source.parent.mkdir(parents=True)
            source.write_text("generated configuration check\n", encoding="utf-8")
            license_path = source.parents[1] / "LICENSE"
            license_path.write_text(
                "Mbed TLS files are provided under a dual "
                "[Apache-2.0](https://spdx.org/licenses/Apache-2.0.html)\n"
                "OR [GPL-2.0-or-later](https://spdx.org/licenses/GPL-2.0-or-later.html) license.\n",
                encoding="utf-8",
            )
            output = root / "cache.json"
            sdk = root / "zephyr-sdk"
            sdk.mkdir()
            build = root / "build"
            build.mkdir()

            self.assertEqual(
                create_cache(root, policy_path, output, {"zephyr-sdk": sdk, "build": build}),
                1,
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document["files"]["modules/crypto/mbedtls/library/mbedtls_config_check_before.h"][
                "license"
            ],
            ["Apache-2.0 OR GPL-2.0-or-later"],
        )

    def test_production_policy_maps_only_named_zephyr_files(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        document = json.loads(policy_path.read_text(encoding="utf-8"))
        zephyr_rules = [
            rule
            for rule in document["rules"]
            if rule.get("source_root", "workspace") == "workspace"
            and rule["path_glob"].startswith("zephyr/")
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

    def test_production_policy_maps_only_reviewed_generated_build_outputs(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        document = json.loads(policy_path.read_text(encoding="utf-8"))
        build_rules = [rule for rule in document["rules"] if rule.get("source_root") == "build"]

        self.assertEqual(
            {
                (rule["path_glob"], rule["license"], rule["evidence_source_root"])
                for rule in build_rules
            },
            {
                ("zephyr/include/generated/device-api-sections.ld", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/*_commit.h", "LicenseRef-Nordic-5-Clause", "workspace"),
                ("zephyr/include/generated/ncs_version.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/pm_config.h", "LicenseRef-Nordic-5-Clause", "workspace"),
                ("zephyr/include/generated/zephyr/*version.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/snippets-*.ld", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/zephyr/autoconf.h", "ISC", "workspace"),
                ("zephyr/include/generated/zephyr/devicetree_generated.h", "BSD-3-Clause", "workspace"),
                ("zephyr/include/generated/zephyr/heap_constants.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/zephyr/kobj-types-enum.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/zephyr/offsets.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/zephyr/syscall_list.h", "Apache-2.0", "workspace"),
                ("zephyr/include/generated/zephyr/syscalls/**/*.h", "Apache-2.0", "workspace"),
                ("zephyr/isr_tables.c", "Apache-2.0", "workspace"),
                ("zephyr/misc/generated/configs.c", "Apache-2.0", "workspace"),
            },
        )

    def test_production_policy_accepts_offset_generator_signature_changes(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        signatures = (
            "def gen_offset_header(input_name, input_file, output_file):",
            "def gen_offset_header(input_file, output_file):",
        )

        for signature in signatures:
            with self.subTest(signature=signature), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "ncs"
                generator = workspace / "zephyr" / "scripts" / "build" / "gen_offset_header.py"
                generator.parent.mkdir(parents=True)
                generator.write_text(
                    "# SPDX-License-Identifier: Apache-2.0\n"
                    "\n"
                    "\"\"\"\n"
                    "This script scans a specified object file and generates a header file\n"
                    "\"\"\"\n"
                    f"{signature}\n"
                    "    return 0\n",
                    encoding="utf-8",
                )
                build = root / "build"
                offsets = build / "zephyr" / "include" / "generated" / "zephyr" / "offsets.h"
                offsets.parent.mkdir(parents=True)
                offsets.write_text("generated offsets\n", encoding="utf-8")
                sdk = root / "zephyr-sdk"
                sdk.mkdir()
                output = root / "cache.json"

                self.assertEqual(
                    create_cache(
                        workspace,
                        policy_path,
                        output,
                        {"build": build, "zephyr-sdk": sdk},
                    ),
                    1,
                )
                document = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(
                    set(document["files"]),
                    {"../build/zephyr/include/generated/zephyr/offsets.h"},
                )
                self.assertEqual(
                    document["files"]["../build/zephyr/include/generated/zephyr/offsets.h"]
                    ["license"],
                    ["Apache-2.0"],
                )

    def test_production_policy_maps_generated_heap_constants(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            source = workspace / "zephyr" / "lib" / "heap" / "heap_constants.c"
            source.parent.mkdir(parents=True)
            source.write_text(
                "/* SPDX-License-Identifier: Apache-2.0 */\n"
                "/* Build-time computation of heap sizing constants from actual struct */\n"
                "/* only used by zephyr_constants_library() to produce the generated */\n"
                "/* heap_constants.h header. */\n",
                encoding="utf-8",
            )
            build = root / "build"
            header = build / "zephyr" / "include" / "generated" / "zephyr" / "heap_constants.h"
            header.parent.mkdir(parents=True)
            header.write_text("generated heap constants\n", encoding="utf-8")
            sdk = root / "zephyr-sdk"
            sdk.mkdir()
            output = root / "cache.json"

            self.assertEqual(
                create_cache(
                    workspace,
                    policy_path,
                    output,
                    {"build": build, "zephyr-sdk": sdk},
                ),
                1,
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document["files"],
            {
                "../build/zephyr/include/generated/zephyr/heap_constants.h": {
                    "license": ["Apache-2.0"],
                    "sha1": sha1(b"generated heap constants\n").hexdigest(),
                }
            },
        )

    def test_production_policy_uses_picolibc_manifests_for_both_sdk_layouts(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        document = json.loads(policy_path.read_text(encoding="utf-8"))
        sdk_rules = [
            rule
            for rule in document["rules"]
            if rule.get("source_root") == "zephyr-sdk" and "picolibc_manifest" in rule
        ]

        self.assertEqual(
            {
                (
                    rule["path_glob"],
                    rule["evidence"]["path"],
                    rule["picolibc_manifest"]["header_root"],
                )
                for rule in sdk_rules
            },
            {
                (
                    "arm-zephyr-eabi/picolibc/include/**/*.h",
                    "arm-zephyr-eabi/share/licenses/picolibc/COPYING.picolibc",
                    "arm-zephyr-eabi/picolibc/include",
                ),
                (
                    "gnu/arm-zephyr-eabi/arm-zephyr-eabi/sys-include/**/*.h",
                    "gnu/arm-zephyr-eabi/share/licenses/picolibc/COPYING.picolibc",
                    "gnu/arm-zephyr-eabi/arm-zephyr-eabi/sys-include",
                ),
            },
        )
        self.assertTrue(all(rule["license"] == "BSD-3-Clause" for rule in sdk_rules))
        self.assertTrue(
            all(
                rule["picolibc_manifest"]["required_license"] == "Default-1"
                for rule in sdk_rules
            )
        )

    def test_production_policy_maps_only_gcc_syslimits_header(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            workspace.mkdir()
            sdk = root / "toolchains" / "zephyr-sdk"
            include = sdk / "gnu" / "arm-zephyr-eabi" / "lib" / "gcc" / "arm-zephyr-eabi" / "14.3.0" / "include"
            include.mkdir(parents=True)
            syslimits = include / "syslimits.h"
            syslimits.write_text("GCC fallback limits header\n", encoding="utf-8")
            (include / "limits.h").write_text("unrelated compiler header\n", encoding="utf-8")
            license_path = sdk / "gnu" / "arm-zephyr-eabi" / "share" / "licenses" / "gcc" / "COPYING.RUNTIME"
            license_path.parent.mkdir(parents=True)
            license_path.write_text(
                "GCC RUNTIME LIBRARY EXCEPTION\n"
                "Version 3.1, 31 March 2009\n"
                "This GCC Runtime Library Exception (\"Exception\") is an additional\n",
                encoding="utf-8",
            )
            build = root / "build"
            build.mkdir()
            output = root / "cache.json"

            self.assertEqual(
                create_cache(
                    workspace,
                    policy_path,
                    output,
                    {"build": build, "zephyr-sdk": sdk},
                ),
                1,
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document["files"],
            {
                "../toolchains/zephyr-sdk/gnu/arm-zephyr-eabi/lib/gcc/arm-zephyr-eabi/14.3.0/include/syslimits.h": {
                    "license": ["GPL-3.0-or-later WITH GCC-exception-3.1"],
                    "sha1": sha1(b"GCC fallback limits header\n").hexdigest(),
                }
            },
        )

    def test_production_policy_maps_only_nrf52840_sdk_runtime_archives(self) -> None:
        policy_path = Path(__file__).parents[1] / "firmware" / "sbom-license-policy.json"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            external_license = (
                workspace
                / "nrf"
                / "scripts"
                / "west_commands"
                / "sbom"
                / "data"
                / "external-licenses"
                / "toolchain.LICENSE"
            )
            external_license.parent.mkdir(parents=True)
            external_license.write_text(
                "SPDX-License-Identifier: GPL-3.0-OR-LATER WITH GCC-EXCEPTION-3.1\n"
                "NCS-SBOM-Apply-To-File: **/toolchains/**/libgcc.a\n",
                encoding="utf-8",
            )
            license_texts = (
                workspace
                / "nrf"
                / "scripts"
                / "west_commands"
                / "sbom"
                / "data"
                / "license-texts.yaml"
            )
            license_texts.write_text(
                "- id: LicenseRef-west-ncs-sbom-picolibc-runtime\n"
                "  text: |\n"
                "    The picolibc runtime archives are built from a collection of source files\n"
                "    that carry multiple licenses and notices.\n"
                "    This LicenseRef represents the bundled multi-license notice set for the\n"
                "    toolchain runtime archives libc.a and libm.a from the picolibc directory.\n",
                encoding="utf-8",
            )
            sdk = root / "toolchains" / "zephyr-sdk"
            gcc_archive = (
                sdk
                / "gnu"
                / "arm-zephyr-eabi"
                / "lib"
                / "gcc"
                / "arm-zephyr-eabi"
                / "14.3.0"
                / "thumb"
                / "v7e-m"
                / "nofp"
                / "space"
                / "libgcc.a"
            )
            libc_archive = (
                sdk
                / "gnu"
                / "arm-zephyr-eabi"
                / "arm-zephyr-eabi"
                / "lib"
                / "thumb"
                / "v7e-m"
                / "nofp"
                / "space"
                / "libc.a"
            )
            for archive, contents in {
                gcc_archive: b"gcc runtime archive\n",
                gcc_archive.parents[3]
                / "v6-m"
                / "nofp"
                / "space"
                / "libgcc.a": b"other target runtime archive\n",
                gcc_archive.with_name("libstdc++.a"): b"unreviewed runtime archive\n",
                libc_archive: b"picolibc runtime archive\n",
                libc_archive.parents[3]
                / "v6-m"
                / "nofp"
                / "space"
                / "libc.a": b"other target libc archive\n",
                libc_archive.with_name("libm.a"): b"unreviewed math archive\n",
            }.items():
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(contents)
            build = root / "build"
            build.mkdir()
            output = root / "cache.json"

            self.assertEqual(
                create_cache(
                    workspace,
                    policy_path,
                    output,
                    {"build": build, "zephyr-sdk": sdk},
                ),
                2,
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document["files"],
            {
                "../toolchains/zephyr-sdk/gnu/arm-zephyr-eabi/arm-zephyr-eabi/lib/"
                "thumb/v7e-m/nofp/space/libc.a": {
                    "license": ["LicenseRef-west-ncs-sbom-picolibc-runtime"],
                    "sha1": sha1(b"picolibc runtime archive\n").hexdigest(),
                },
                "../toolchains/zephyr-sdk/gnu/arm-zephyr-eabi/lib/gcc/arm-zephyr-eabi/"
                "14.3.0/thumb/v7e-m/nofp/space/libgcc.a": {
                    "license": ["GPL-3.0-or-later WITH GCC-exception-3.1"],
                    "sha1": sha1(b"gcc runtime archive\n").hexdigest(),
                },
            },
        )

    def test_picolibc_manifest_maps_only_exact_default_records(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            workspace.mkdir()
            sdk = root / "toolchains" / "zephyr-sdk"
            manifest = sdk / "licenses" / "COPYING.picolibc"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                "Files: *\n"
                "License: Default-1\n\n"
                "Files: newlib/libc/include/limits.h\n"
                " newlib/libc/include/machine/_time.h\n"
                " newlib/libc/include/tagged.h\n"
                "License: Default-1\n\n"
                "Files: newlib/libc/include/wildcard/*\n"
                "License: Other-1\n\n"
                "Files: picolibc.h.in\n"
                "License: Default-1\n\n"
                "Files: newlib/libc/include/not-default.h\n"
                " newlib/libc/include/picolibc.h\n"
                "License: Other-1\n\n"
                "License: Default-1\n"
                " BSD three-clause evidence\n",
                encoding="utf-8",
            )
            header_root = sdk / "toolchain" / "include"
            (header_root / "machine").mkdir(parents=True)
            for relative_path, contents in {
                "limits.h": "no SPDX tag\n",
                "machine/_time.h": "no SPDX tag\n",
                "picolibc.h": "generated header\n",
                "tagged.h": "/* SPDX-License-Identifier: MIT */\n",
                "not-default.h": "other manifest license\n",
                "not-listed.h": "no manifest source record\n",
                "wildcard/unapproved.h": "wildcard manifest override\n",
            }.items():
                path = header_root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule(
                            "toolchain/include/**/*.h",
                            "BSD-3-Clause",
                            "licenses/COPYING.picolibc",
                            source_root="zephyr-sdk",
                            evidence_contains=[
                                "License: Default-1\n BSD three-clause evidence"
                            ],
                            picolibc_manifest={
                                "header_root": "toolchain/include",
                                "source_prefix": "newlib/libc/include",
                                "required_license": "Default-1",
                                "generated_sources": {"picolibc.h": "picolibc.h.in"},
                            },
                        )
                    ]
                ),
                encoding="utf-8",
            )
            output = root / "cache.json"

            self.assertEqual(
                create_cache(workspace, policy, output, {"zephyr-sdk": sdk}), 4
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            set(document["files"]),
            {
                "../toolchains/zephyr-sdk/toolchain/include/limits.h",
                "../toolchains/zephyr-sdk/toolchain/include/machine/_time.h",
                "../toolchains/zephyr-sdk/toolchain/include/not-listed.h",
                "../toolchains/zephyr-sdk/toolchain/include/picolibc.h",
            },
        )
        self.assertTrue(
            all(entry["license"] == ["BSD-3-Clause"] for entry in document["files"].values())
        )

    def test_rejects_picolibc_manifest_without_explicit_license_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule(
                            "include/**/*.h",
                            "BSD-3-Clause",
                            "licenses/COPYING.picolibc",
                            evidence_contains=["unrelated evidence"],
                            picolibc_manifest={
                                "header_root": "include",
                                "source_prefix": "newlib/libc/include",
                                "required_license": "Default-1",
                                "generated_sources": {},
                            },
                        )
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SbomLicenseCacheError, "evidence must explicitly verify 'Default-1'"
            ):
                create_cache(root, policy, root / "cache.json")

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

    def test_creates_cache_for_reviewed_external_source_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            workspace.mkdir()
            sdk = root / "toolchains" / "zephyr-sdk"
            evidence = sdk / "licenses" / "COPYING"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            source = sdk / "include" / "generated.h"
            source.parent.mkdir()
            source.write_bytes(b"external generated source")
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule(
                            "include/*.h",
                            "Apache-2.0",
                            "licenses/COPYING",
                            source_root="zephyr-sdk",
                        )
                    ]
                ),
                encoding="utf-8",
            )
            output = root / "cache.json"

            self.assertEqual(
                create_cache(workspace, policy, output, {"zephyr-sdk": sdk}), 1
            )

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "files": {
                    "../toolchains/zephyr-sdk/include/generated.h": {
                        "license": ["Apache-2.0"],
                        "sha1": sha1(b"external generated source").hexdigest(),
                    }
                }
            },
        )

    def test_creates_cache_for_generated_output_with_workspace_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ncs"
            workspace.mkdir()
            evidence = workspace / "scripts" / "generator.py"
            evidence.parent.mkdir()
            evidence.write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
            build = root / "candidate" / "build"
            source = build / "include" / "generated.h"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"generated build output")
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule(
                            "include/generated.h",
                            "Apache-2.0",
                            "scripts/generator.py",
                            source_root="build",
                            evidence_source_root="workspace",
                        )
                    ]
                ),
                encoding="utf-8",
            )
            output = root / "cache.json"

            self.assertEqual(create_cache(workspace, policy, output, {"build": build}), 1)

            document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "files": {
                    "../candidate/build/include/generated.h": {
                        "license": ["Apache-2.0"],
                        "sha1": sha1(b"generated build output").hexdigest(),
                    }
                }
            },
        )

    def test_rejects_unconfigured_external_source_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "component" / "generated.h"
            source.parent.mkdir()
            source.write_text("generated\n", encoding="utf-8")
            (root / "component" / "LICENSE").write_text(
                "SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8"
            )
            policy = root / "policy.json"
            policy.write_text(
                _policy(
                    [
                        _rule(
                            "component/*.h",
                            "Apache-2.0",
                            "component/LICENSE",
                            source_root="zephyr-sdk",
                        )
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SbomLicenseCacheError, "source root 'zephyr-sdk' is not configured"):
                create_cache(root, policy, root / "cache.json")

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
