from __future__ import annotations

import json
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.create_firmware_provenance import ProvenanceError, create_provenance
from tools.validate_sbom import SbomError, validate_spdx


class FirmwareEvidenceTests(unittest.TestCase):
    def test_accepts_spdx_with_concluded_file_licenses(self) -> None:
        with TemporaryDirectory() as directory:
            report = Path(directory) / "firmware.spdx"
            report.write_text(
                "\n".join(
                    (
                        "SPDXVersion: SPDX-2.2",
                        "PackageName: nrf",
                        "PackageLicenseConcluded: NOASSERTION",
                        "",
                        "FileName: nrf/LICENSE",
                        "SPDXID: SPDXRef-file-1",
                        "LicenseConcluded: LicenseRef-Nordic-5-Clause",
                        "LicenseInfoInFile: NOASSERTION",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            validate_spdx(report)

    def test_rejects_unknown_concluded_file_licenses(self) -> None:
        with TemporaryDirectory() as directory:
            report = Path(directory) / "firmware.spdx"
            report.write_text(
                "\n".join(
                    (
                        "SPDXVersion: SPDX-2.2",
                        "FileName: generated.h",
                        "LicenseConcluded: NOASSERTION",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SbomError, r"generated\.h: NOASSERTION"):
                validate_spdx(report)

    def test_rejects_placeholder_license_regardless_of_case(self) -> None:
        with TemporaryDirectory() as directory:
            report = Path(directory) / "firmware.spdx"
            report.write_text(
                "SPDXVersion: SPDX-2.2\nFileName: generated.h\nLicenseConcluded: noassertion\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SbomError, r"generated\.h: noassertion"):
                validate_spdx(report)

    def test_rejects_file_without_a_concluded_license(self) -> None:
        with TemporaryDirectory() as directory:
            report = Path(directory) / "firmware.spdx"
            report.write_text(
                "SPDXVersion: SPDX-2.2\nFileName: generated.h\nSPDXID: SPDXRef-file-1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SbomError, "without LicenseConcluded"):
                validate_spdx(report)

    def test_records_resolved_build_evidence_for_the_exact_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "rcp.elf"
            artifact.write_bytes(b"firmware")
            metadata = root / "release-metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "artifact": artifact.name,
                        "ncs_version": "3.4.0",
                        "zephyr_version": "4.4.0",
                        "sha256": sha256(artifact.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            ncs_license = root / "NCS-LICENSE.txt"
            ncs_license.write_text("NCS license\n", encoding="utf-8")
            west_manifest = root / "west-manifest.yml"
            west_manifest.write_text("manifest:\n", encoding="utf-8")
            toolchain = root / "toolchain.txt"
            toolchain.write_text("Zephyr SDK 0.16\n", encoding="utf-8")
            sbom_policy = root / "sbom-license-policy.json"
            sbom_policy.write_text('{"schema_version":1}\n', encoding="utf-8")
            sbom_cache = root / "sbom-license-cache.json"
            sbom_cache.write_text('{"files":{}}\n', encoding="utf-8")
            output = root / "provenance.json"

            create_provenance(
                metadata,
                artifact,
                "v3.4.0",
                "0" * 40,
                ncs_license,
                west_manifest,
                toolchain,
                sbom_policy,
                sbom_cache,
                "1" * 40,
                output,
            )

            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["artifact"]["sha256"], sha256(b"firmware").hexdigest())
            self.assertEqual(document["ncs"]["tag"], "v3.4.0")
            self.assertEqual(document["ncs"]["revision"], "0" * 40)
            self.assertEqual(
                document["sbom"]["license_policy_sha256"],
                sha256(sbom_policy.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                document["sbom"]["license_cache_sha256"],
                sha256(sbom_cache.read_bytes()).hexdigest(),
            )

    def test_rejects_provenance_for_an_unexpected_artifact_name(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "actual.elf"
            artifact.write_bytes(b"firmware")
            metadata = root / "release-metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "artifact": "expected.elf",
                        "ncs_version": "3.4.0",
                        "zephyr_version": "4.4.0",
                        "sha256": sha256(artifact.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            source = root / "source"
            source.write_text("evidence\n", encoding="utf-8")

            with self.assertRaisesRegex(ProvenanceError, "name does not match"):
                create_provenance(
                    metadata,
                    artifact,
                    "v3.4.0",
                    "0" * 40,
                    source,
                    source,
                    source,
                    source,
                    source,
                    "0" * 40,
                    root / "out",
                )


if __name__ == "__main__":
    unittest.main()
