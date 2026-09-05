# Runtime Component Inventory

This image contains the PCA10059 OpenThread RCP Updater application, the
`nrfdfu-rs` Secure DFU utility, and packages supplied by the selected Home
Assistant Debian base image.

- `/usr/share/licenses/pca10059-openthread-rcp-updater/LICENSE` is the
  Apache-2.0 license for this application.
- `/usr/share/licenses/nrfdfu-rs/LICENSE-APACHE` and `NOTICE.md` cover the
  locally patched `nrfdfu-rs` binary. The project selects its Apache-2.0
  licensing option.
- `/usr/share/licenses/nrfdfu-rs/dependency-metadata.json` is Cargo's locked
  package graph for the exact nrfdfu-rs binary built in this image. It records
  each Rust package's declared license metadata where supplied upstream.
- `/usr/share/licenses/pca10059-openthread-rcp-updater/debian-packages.tsv`
  is the installed Debian package and version inventory captured at image
  build time.

The inventories are evidence for review, not a substitute for the complete
license texts or notices required by individual dependencies. The NCS-derived
firmware is not part of this image; each published ELF carries its own NCS
license, SPDX SBOM, generated notice, and provenance record.
