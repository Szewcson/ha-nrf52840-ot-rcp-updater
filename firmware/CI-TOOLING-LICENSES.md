# CI Tooling License Review

Reviewed: 2026-09-05

This record covers tools that run only in the disposable GitHub Actions build
environment. Their executables, virtual environments, and package data are not
copied into the add-on image or the `firmware` branch. The firmware itself is
separately subject to the NCS license and the per-release SPDX SBOM, notices,
and provenance record. This is an engineering record, not legal advice.

| Tool or dependency | License position | Role |
| --- | --- | --- |
| [west 1.4.0](https://github.com/zephyrproject-rtos/west) | Apache-2.0 | Resolves and builds the NCS workspace. |
| [Zephyr SDK](https://github.com/zephyrproject-rtos/sdk-ng) | The project is Apache-2.0; bundled components retain their own terms. | CI compiler and host tools; the resulting firmware inputs are accounted for by the release SBOM. |
| [Jinja2](https://pypi.org/project/Jinja2/) | BSD-3-Clause | NCS native SBOM dependency. |
| [fingerprints 1.2.3](https://pypi.org/project/fingerprints/) | MIT | NCS native SBOM dependency. |
| [normality 2.5.0](https://pypi.org/project/normality/) | MIT | NCS native SBOM dependency; its own resolved dependencies remain CI-only. |
| [ScanCode Toolkit 32.4.1](https://github.com/aboutcode-org/scancode-toolkit) | Apache-2.0 software, CC-BY-4.0 reference data, plus its listed third-party terms. | Optional NCS license detector, installed and used only in CI. |
| `commoncode 32.3.0` and `click 8.2.1` | Respectively Apache-2.0 and BSD-3-Clause. | Compatibility pins required for ScanCode 32.4.1 in this workflow. |
| [Ruff](https://github.com/astral-sh/ruff) | MIT | Source linting in verification jobs. |
| [cryptography 43.0.0](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause | Signs firmware-manifest entries in the publication job. |

## ScanCode Handling

NCS makes its `scancode-toolkit` detector optional, and this workflow enables
it for additional license-text recognition. ScanCode's code is Apache-2.0, but
its distribution also includes CC-BY reference data and heterogeneous
third-party material. The tool, its virtual environment, and its database stay
in the disposable CI runner: only firmware evidence generated from the NCS
source build is published. Do not copy ScanCode or a substantial part of its
reference data into the add-on image or firmware branch without a new
redistribution review that preserves its notices, attribution, and applicable
third-party terms.
