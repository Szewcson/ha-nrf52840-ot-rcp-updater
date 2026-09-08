# PCA10059 OpenThread RCP Updater

Home Assistant app that exposes a native `update` entity for the PCA10059
nRF52840 dongle used as the RCP for the Home Assistant Core OpenThread Border
Router app. It never replaces OTBR: the app performs one controlled hand-off
from OTBR to the stock PCA10059 Secure DFU bootloader and back.

This is an independent community project. It is not affiliated with, sponsored
by, authorized by, or endorsed by Nordic Semiconductor ASA, Home Assistant,
Nabu Casa, the Open Home Foundation, or the OpenThread project. Those names
are used only to identify hardware, source, and product compatibility.

## Version Contract

`SPINEL_PROP_NCP_VERSION` already returns OpenThread's standard string:

```text
PACKAGE_NAME/PACKAGE_VERSION; OPENTHREAD_CONFIG_PLATFORM_INFO; BUILD_DATETIME
```

This project changes only `OPENTHREAD_CONFIG_PLATFORM_INFO` during the RCP
build. The required value is:

```text
NRF52840 PCA10059 N/<ncs-version> Z/<zephyr-version>
```

`NRF52840` is the leading bare platform token used by the upstream
[nRF52840 port](https://github.com/openthread/ot-nrf528xx/blob/main/src/nrf52840/openthread-core-nrf52840-config.h#L56-L63),
and matches the same shape used by the upstream
[EFR32 port](https://github.com/openthread/ot-efr32/blob/main/src/src/openthread-core-efr32-config.h#L186-L193).
OpenThread documents platform info only as a platform-specific string, without
defining its syntax or length in the
[configuration reference](https://github.com/openthread/openthread/blob/main/src/core/config/platform.h#L50-L56).
`PCA10059`, `N`, and `Z` are compact project-defined board, NCS, and Zephyr
tags used by this updater's verification. They deliberately avoid the former
`HW/`, `NCS/`, and `ZEPHYR/` spellings, which left too little room for longer
NCS-generated package versions and build timestamps. The parser still accepts
those expanded legacy tags when identifying older project firmware.

The builder enforces these compatibility rules:

- Platform info is ASCII-only because Home Assistant's
  [Universal Silabs Flasher Spinel probe](https://github.com/NabuCasa/universal-silabs-flasher/blob/dev/universal_silabs_flasher/spinel.py#L336-L342)
  decodes the full property as ASCII.
- Platform info cannot contain `;`, because OpenThread uses `; ` to separate
  its package, platform, and optional build-time fields in
  [`otGetVersionString`](https://github.com/openthread/openthread/blob/main/src/core/api/instance_api.cpp#L185-L189).
- Platform info cannot contain `_`. This is this project's explicit naming
  policy, not an OpenThread or Universal Silabs Flasher requirement; that
  flasher only treats `_` as a separator in the *package-version* field it
  keeps before the first `;`.
- The exact NUL-terminated string from the generated ELF must be ASCII and at
  most 127 bytes before its terminator. OpenThread's host Spinel driver stores
  it in [`char mVersion[128]`](https://github.com/openthread/openthread/blob/main/src/lib/spinel/spinel_driver.hpp#L238-L239)
  and rejects an unsuccessful decode rather than offering a truncation policy.
  The check budgets the generated package version and optional build datetime,
  not only the project-controlled platform-info field.

No custom Spinel property is introduced. No `FW/...` tag is added: the existing
OpenThread package version and build timestamp are retained. The updater parser
reads compact `PCA10059`, `N`, and `Z` tags only from the platform-info field,
accepts the new leading token, and continues to accept the pre-existing
expanded token-less form so older project builds can still be identified.

The Home Assistant update entity compares the reported `NCS` field with the
newest matching entry in the firmware branch's static manifest. Hardware and
Zephyr fields must also match after flashing.

## Update Flow

1. Home Assistant discovers `PCA10059 OpenThread RCP` as an MQTT `update` entity.
2. A normal `update.install` request, or an explicitly confirmed operation in
   the Ingress panel, enters the same single-operation controller and the same
   `RcpUpdater` transaction. There is no separate web flasher.
3. A signed release verifies its manifest, detached Ed25519 signature, SHA-256,
   32-bit little-endian ARM ELF headers, and PCA10059 flash segments before
   OTBR is stopped. A URL or uploaded ELF receives the same bounded structural
   and tag validation, but cannot claim the release signature's authenticity.
4. With `safe_update: true`, the app checks OTBR REST management actions twice
   over the configured quiet window when OTBR is running. This detects
   commissioning or diagnostic work, but cannot prove that all Thread
   application traffic is idle. If OTBR was explicitly stopped before the
   request, it is treated as an offline maintenance window: REST checks are
   omitted and OTBR is left stopped after the update.
5. The app stops OTBR through the Supervisor API, records the RCP's physical
   USB topology, exclusively opens the Spinel device, asks for
   `SPINEL_RESET_BOOTLOADER`, waits for Nordic `1915:521f` on that same physical
   USB port, and programs its ELF with `nrfdfu --serial --port --fw-version --abort`.
   The bootloader descriptor serial is discovered internally only after the
   topology check and the command returns the dongle to the RCP application.
6. It verifies `HW`, `NCS`, and `ZEPHYR` through `SPINEL_PROP_NCP_VERSION`,
   restarts OTBR in a `finally` block, waits for its REST health endpoint, and
   records state only after successful verification. A failed install clears
   the recorded version, so Home Assistant reports `unknown` rather than a
   potentially stale firmware version.

The update entity and sidebar report the current verified operation phase
throughout this flow. `nrfdfu` does not expose byte-level transfer progress, so
the UI uses an indeterminate activity indicator rather than invented
percentages; `100%` is published only after post-flash Spinel verification.

MQTT control messages are deliberately one-shot. The updater ignores retained
commands and QoS redeliveries marked as duplicates, so a broker replay cannot
repeat a firmware operation after an app restart or reconnect. A recovery
attempt must come from a new explicit Home Assistant action.

The update entity is compatible with Home Assistant automations. This project
does not run a hidden scheduler in the add-on; an automation can call
`update.install` at a maintenance window after you decide the traffic policy.

## Sidebar / Ingress

The app adds the admin-only **PCA10059 RCP** sidebar panel through Home
Assistant Ingress. It has no host or LAN port mapping: the server listens on
the app's internal port `8099` and accepts requests only from Home Assistant's
Ingress proxy (`172.30.32.2`), as required by the
[Home Assistant Ingress guidance](https://developers.home-assistant.io/docs/apps/presentation/#ingress).
Home Assistant authenticates the user before proxying the request.

The panel shows the configured serial device, normal USB VID:PID and serial,
RCP/DFU state, persisted verified NCS and Zephyr versions, current policy
target, OTBR state, operation phase, and a bounded recent-operation log. It
offers three deliberate sources:

- **Release** downloads a release listed in the signed manifest.
- **URL** downloads one public HTTPS URL, follows at most three HTTPS redirects,
  rejects credentials, non-default ports, and DNS results that are loopback,
  link-local, private, or otherwise non-global, then pins the approved address
  for the TLS connection to prevent DNS rebinding.
- **Upload** streams a raw local file to a server-generated `/tmp/ingress-*.elf`
  name. The browser-provided filename is ignored.

Only an application ELF is supported. HEX, BIN, ZIP, and arbitrary archive
formats are rejected. Every manual ELF is capped at 4 MiB, parsed as a
PCA10059 ARM ELF, and must contain exactly one project platform tag:

```text
NRF52840 PCA10059 N/<ncs-version> Z/<zephyr-version>
```

The tag lets the updater derive the Secure DFU application version and requires
the post-flash Spinel identity to match the requested firmware. A manually
supplied URL or upload is still an operator-trusted input: it receives SHA-256,
structural, hardware, and embedded-tag checks, but it is not an Ed25519-signed
project release. The UI makes that distinction explicit.

Validated manual files are held only in the `/tmp` tmpfs with mode `0600`, are
removed after a completed or failed operation, and expire after 15 minutes even
if the browser stops polling. The Flash control only receives an unguessable
server-side validation token, never a path. Preflight shows confirmed checks as
successful, reports bootloader transition as pending when it can only be tested
during the protected hand-off, and enables recovery when the normal serial
device is absent only after the existing exact Secure DFU selector succeeds.

The former MQTT **Manual RCP firmware target** select and **Flash selected RCP
firmware** button are removed. On startup the app publishes empty retained
discovery payloads for them, so Home Assistant removes the obsolete entities.
The native MQTT `update` entity remains available for ordinary updates.

## Runtime Image

The app uses Home Assistant's official Alpine base image. `nrfdfu` is compiled
natively in a pinned Rust musl builder and smoke-tested in the final image, so
the runtime does not depend on a glibc compatibility layer.

The Rust builder image is pinned by its multi-architecture content digest. The
runtime `BUILD_FROM` value remains architecture-selected by the Home Assistant
add-on build convention, so its upstream update policy is reviewed separately.
The final image includes this project's Apache-2.0 license, the nrfdfu-rs
license and notice, Cargo's locked dependency metadata, and an installed Alpine
package inventory under `/usr/share/licenses`.

The app declares Home Assistant's `manager` Supervisor role because it must
stop and restart OTBR around a DFU operation. Home Assistant only grants that
capability to an app with extended Supervisor access.

The add-on includes an explicit custom AppArmor profile. Its s6/Bashio launcher
has only DNS and TCP/UDP network access; it does not receive a blanket Linux
capability grant. It starts a separate confined Python profile, which can read
its application files and USB topology, write only its state under `/data`, use
the RCP serial interfaces, and make the connections needed for HTTPS, MQTT,
OTBR, and the Supervisor. `nrfdfu` runs in a further profile that has no network
permission and only the sysfs traversal needed to enumerate its selected CDC
ACM port. `/tmp` is a container tmpfs, so temporary files cannot persist across
app restarts. Home Assistant applies the resulting security-rating point after
installation. The profile is loaded when the add-on starts, so restart the app
after an upgrade before retrying a DFU recovery.

With the profile active, Ingress and AppArmor offset the required `manager`
role, so the expected Supervisor score is the maximum reported `6`. A lower
score means the custom profile is not active on that Home Assistant host. The
app does not use host networking or a broader Home Assistant API role merely to
raise the rating.

The Home Assistant Core OpenThread Border Router app uses host networking,
while this app uses Home Assistant's internal app network. Home Assistant's
OTBR service alias, `core-openthread-border-router`, works across that boundary,
so the safe-update preflight reaches
`http://core-openthread-border-router:8081` without exposing anything on the
host. Leave OTBR's **OpenThread REST API port** disabled under **Network**
unless you separately need its advanced debugging interface. An existing `8081`
host-port mapping can be removed after this app is upgraded and restarted; the
updater does not use it.

If OTBR reports `started` or `running`, an unreachable REST API is a safety
failure and the update is rejected. To update while the REST API is
unavailable, explicitly stop the OTBR app first, wait for its state to become
`stopped`, then request the update. The updater will not restart an app that
was already stopped; start OTBR manually after the RCP has been verified.

## Runtime Tool Licensing

This repository's source code is licensed under Apache-2.0. The RCP firmware is
built from the nRF Connect SDK (NCS) for an nRF52840, so its distribution also
remains subject to the [NCS license](https://github.com/nrfconnect/sdk-nrf/blob/main/LICENSE)
and the notices of its transitive modules. Every published ELF is accompanied
by the exact NCS license text, an SPDX SBOM, its generated human-readable
notice, and a provenance record binding it to its source revision, resolved
west manifest, SDK report, and SHA-256.

### CI-only Build and SBOM Tooling

The firmware workflow runs `west`, the release-matched Zephyr SDK, and NCS's
native SBOM dependencies only in disposable GitHub Actions environments. Their
code, virtual environments, and package data are not copied into the add-on
image or firmware branch. The generated SBOM and notices remain release
evidence, not a redistribution of those tools. The maintained review of their
licenses and role is in [firmware/CI-TOOLING-LICENSES.md](firmware/CI-TOOLING-LICENSES.md).
This is an engineering record, not legal advice.

The runtime updater contains no Nordic `nrfutil`, `nrfutil device`,
`nrf-device-lib`, or `nrf5sdk-tools` binary. It builds `nrfdfu-rs` from pinned
upstream commit `8a8e0adda6b0f44cf1ba9aee62c62a67df335137` for the target
container architecture. Two focused local patches expose Secure DFU firmware
and hardware-version CLI options and accept an exact serial endpoint. The
updater resolves the stock Nordic `1915:521f` bootloader from Linux USB
topology before it invokes `nrfdfu`, then passes that exact endpoint so a
duplicate device serial cannot select another USB interface. The bootloader
serial is an internal transport selector, not a required setting.
Bootloader-only recovery accepts one VID:PID target, or an optional
`dfu_serial_number` when multiple compatible devices are attached. When
multiple targets expose the same descriptor serial, the optional
`dfu_usb_path` instead selects one Linux USB topology name (for example,
`2-3`). The Docker build checks that each patch applies before building. `nrfdfu-rs` is licensed
`MIT OR Apache-2.0`; this project uses the Apache-2.0 option and includes its
notice and license in the add-on image.

The published artifact is an application-only ELF. It does not include a
Nordic SoftDevice. This is an implementation note, not legal advice. Review
Nordic's current terms before changing the distribution model or NCS dependency
set. Names associated with NCS, Nordic hardware, Home Assistant, and
OpenThread identify compatibility only and do not imply endorsement.

### Firmware SBOM Policy

NCS labels build-directory SBOM analysis as experimental. Its NCS 3.3 extractor
mistakenly treats Git bookkeeping as build input, so the publisher applies the
same narrow `.git` exclusion already present upstream in NCS 3.4. It verifies
that `nrf` and `zephyr` are real Git work trees before doing so. A second
reviewed patch excludes only local `*.elf` and `linker*.cmd` products from the
source-input report: they are intermediate link outputs, while their source,
archive, and linker-script inputs remain in the SBOM. It cannot exclude a
prebuilt ELF or linker command outside the selected build directory.

Some real build inputs have no per-file SPDX tag: NCS version metadata,
generated Mbed TLS configuration checks, MPSL FEM precompiled archives, named
Zephyr metadata, empty-compilation-unit, and linker-script files, as well as
known NCS/Zephyr generator outputs for version, commit, configuration,
devicetree, syscall, linker-snippet, kernel-object, offset, and ISR data.
`firmware/sbom-license-policy.json` maps only those reviewed path patterns
through NCS's hash-bound cache-database mechanism after checking the upstream
generator or license evidence for each mapping. The policy, resulting cache,
and their hashes in the provenance record are published beside the SBOM. A new
generator family, artifact path, or license change therefore blocks unattended
publication for review instead of receiving a project-local license guess. The
release job still rejects every other `NOASSERTION`, `NONE`, or unknown file
license rather than silently publishing it. See the
[NCS SBOM documentation](https://github.com/nrfconnect/sdk-nrf/blob/main/scripts/west_commands/sbom/README.rst)
for its build-input and cache-database model.

The workflow installs the Zephyr SDK below `toolchains/`, as required by NCS's
built-in, reviewed external-file rules for the GCC runtime and Picolibc static
runtime archives. It verifies those exact NCS rules before running the SBOM.
For both supported SDK layouts, the cache generator reads Picolibc's bundled
`COPYING.picolibc` manifest and applies its ordered `Files` rules using the
[DEP-5 specification's documented "last matching stanza wins" behavior](https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/#files).
It maps a header only when that final source record is labeled `Default-1`; the
manifest's complete `Default-1` text proves the BSD-3-Clause conclusion. It
skips headers with their own SPDX declaration and any source record with an
unknown or unsupported license expression. This accommodates added or removed
Picolibc headers without turning a future SDK change into a project-local
license guess. The generated `picolibc.h` is explicitly traced to its
`picolibc.h.in` source record. Any unsupported SDK layout or unproven license
still fails closed during SBOM validation.

NCS runs detectors from left to right. Its current cache detector assigns the
cache's JSON license list directly to the internal license collection; if a
later detector updates that collection, affected NCS releases raise an
`AttributeError`. Native SPDX, text, external-file, and Git detectors therefore
run first. The optional cache then fills only files still without a conclusion;
optional ScanCode runs last and skips cache-matched files. No mutating detector
runs after the cache, so native conclusions remain authoritative without
triggering that list/set mismatch. This ordering is verified against NCS 3.3.4
through 3.5.0-preview1 and uses NCS's documented optional-detector behavior.

NCS lists ScanCode as an optional SBOM detector. The workflow enables it only
in its disposable CI environment for independent license-text recognition and
pins the compatible dependency versions required by ScanCode 32.4.1. ScanCode
and its reference database are not copied into the add-on image or firmware
branch; its detector output is still subject to the same fail-closed validation.
See [firmware/CI-TOOLING-LICENSES.md](firmware/CI-TOOLING-LICENSES.md) for the
tooling-license review and redistribution boundary.

## Configuration

Choose the normal RCP serial device with Home Assistant's serial-device picker.
Use a stable `/dev/serial/by-id/...` path when it is available. Select and save
this device while the normal RCP application is running: Secure DFU exposes a
different USB product and tty endpoint, so it is intentionally not offered by
the normal-RCP picker.

```yaml
device: /dev/serial/by-id/usb-Nordic_Semiconductor_Thread_Co-Processor-if00
# Everything below has this built-in default and is optional to configure.
baudrate: "1000000"
safe_update: true
# Opt-in workaround for QEMU direct USB passthrough during RCP/DFU transitions.
qemu_usb_reenumeration_workaround: false
allow_legacy_rcp: false
allow_prereleases: false
# Keep normal updates within one NCS major.minor line, for example: 3.4
pinned_ncs_minor:
# Optional: disambiguates bootloader-only recovery when several matching dongles exist.
dfu_serial_number:
# Optional: disambiguates identical DFU serials by Linux USB topology, for example 2-3.
dfu_usb_path:
manifest_poll_interval: 3600
idle_window: 20
# Post-DFU RCP verification waits at least 90 seconds; larger values are honored.
boot_timeout: 90
```

`baudrate` is a fixed Home Assistant selector matching the Core OpenThread Border Router
frontend choices: `57600`, `115200`, `230400`, `460800`, `921600`, and
`1000000`. The PCA10059 build uses USB CDC ACM, so this app intentionally does
not expose a hardware-flow-control setting: Nordic's flow-control guidance is
for UART-connected development kits, not this USB dongle. Use the same baudrate
as OTBR; current project firmware uses `1000000` by default.

### QEMU Direct USB Passthrough

Leave `qemu_usb_reenumeration_workaround` disabled unless a QEMU guest loses
the RCP during its normal-RCP <-> Secure-DFU USB identity transitions. When
enabled, the app waits eight seconds after requesting Secure DFU and again
after asking Secure DFU to boot the application, before making the first
`nrfdfu` or Spinel probe. The update entity shows each wait as a progress stage.

This is a bounded mitigation for QEMU's direct `usb-host` backend, not a claim
that a fixed delay repairs every passthrough failure. In QEMU 11.0.3,
auto-selected USB devices are scanned every two seconds; a product ID of zero
is a wildcard, failed opens are capped at three attempts, and a still-open
handle prevents a replacement device from being opened. QEMU closes that
handle and immediately rescans after guest I/O reports `NO_DEVICE`. Waiting
for the replacement USB personality to be ready before that first guest probe
avoids the observed early-rescan race. The eight-second delay spans four scan
intervals. See QEMU's
[auto-selection implementation](https://github.com/qemu/qemu/blob/v11.0.3/hw/usb/host-libusb.c#L1834-L1920)
and its
[NO_DEVICE recovery path](https://github.com/qemu/qemu/blob/v11.0.3/hw/usb/host-libusb.c#L1102-L1148).
It increases the OTBR outage by at least 16 seconds per normal flash; leave it
off for bare metal, controller passthrough, or stable direct passthrough.

This app intentionally supports one target: a PCA10059 RCP, its stock Nordic
Secure DFU bootloader (`1915:521f`), and this repository's verified firmware
manifest. It also supports only Home Assistant's Core OpenThread Border Router
app, `core_openthread_border_router`, using its internal service alias at
`http://core-openthread-border-router:8081` for quiet-window and post-flash
health checks. These are not configuration choices. On first startup after
this upgrade, the app removes the retired `hardware`, `manifest_url`,
`otbr_addon_slug`, `otbr_api_url`, and `dfu_vid_pid` options through the
Supervisor API.

For a normal update, the updater records the configured RCP's physical USB port
before it resets into DFU, then accepts only the matching VID:PID on that same
port. This avoids relying on a volatile `/dev/ttyACM*` name, assumes neither
USB personality has the same descriptor serial, and excludes other Nordic DFU
dongles attached to different ports.

Leave `dfu_serial_number` and `dfu_usb_path` empty for one dongle. They are
advanced overrides for bootloader-only recovery: when the normal RCP path is
absent, exactly one `1915:521f` device is accepted automatically. If several
targets have different descriptor serials, set `dfu_serial_number`. If they
share a serial, set `dfu_usb_path` to the topology name reported by the error,
such as `2-3`; do not guess this value, because it authorizes that physical USB
port to be flashed.

The app requests Home Assistant UART access because the normal RCP and its
Secure DFU personality both use CDC ACM serial endpoints. It does not request
raw USB access: `nrfdfu` is bound to one validated serial endpoint. When a
selected DFU target is unavailable, the update entity's `dfu_probe_output`
diagnostic and update error show a bounded discovery result. This distinguishes
a missing USB device from a DFU tool or permission failure without allowing a
flash to an unknown target.

If a failed update leaves the dongle in Secure DFU mode, keep the normal RCP
`device` value unchanged and open the **PCA10059 RCP** sidebar panel. Validate
the exact release you want to recover, then confirm Flash. The preflight uses
the same constrained selector as the transaction: it accepts exactly one
`1915:521f` bootloader and never treats PID `0000` as a wildcard. It skips the
Spinel reset only after that exact Secure DFU target is confirmed, and still
requires the normal post-flash Spinel version check. Set
`dfu_serial_number` only if several bootloaders match with distinct serials,
or `dfu_usb_path` if their serials are identical. If the exact DFU target
remains present after the normal verification window expires, the updater sends
one application reboot and makes one final verification attempt; it never
flashes the image twice.

The panel's **Release** tab lists known signed releases in the configured
stable or prerelease channel, including older ones for explicit recovery or
rollback. **URL** and **Upload** are separate operator-controlled sources and
are deliberately not exposed through MQTT. The old MQTT selector and button
were removed; after an app upgrade their retained discovery records are cleared
automatically. The native Update entity continues to expose the normal
automatic policy, its current phase, and diagnostics such as device path,
normal-RCP USB serial, configured DFU VID:PID, topology state, DFU presence,
and OTBR state.

The Update entity always reports `installed_version` from the last verified
Spinel response and `latest_version` from the automatic release policy. After a
deliberate rollback, Home Assistant can regard the lower target as up to date
or hide its Install control. The sidebar panel is the reliable interface for
that exact lower release; it displays installed and policy-target versions
separately.

After startup, the updater waits briefly before attempting one guarded version
rescan. It retries only while OTBR is starting, stopping, unavailable, or busy;
it does not stop OTBR in those states. Once OTBR is stable and passes the same
quiet-window check used for updates, the updater reads
`SPINEL_PROP_NCP_VERSION` and replaces its state only with complete matching
`HW`, `NCS`, and `ZEPHYR` tags. A failed Spinel read reports `unknown` rather
than retaining old state. If `nrfdfu` reports a non-zero transfer result after
it has started the image, the updater does not assume that the RCP is unchanged:
it waits for a normal Spinel response and records the requested version only
when all three tags match.

The app reloads its built-in firmware manifest every hour by default and updates the native HA
entity when a newer NCS release is published. To install automatically, create
a Home Assistant automation for that entity which calls `update.install` in a
maintenance window; the actual RCP comparison still happens through Spinel
immediately before flashing.

Keep `allow_legacy_rcp` false. A legacy RCP without the three tags requires an
explicit opt-in because its hardware cannot be verified before an ordinary
policy update. With `allow_legacy_rcp: true`, the native Update entity uses the
newest release in the selected stable or prerelease channel and optional minor
pin. An explicitly validated sidebar target is safe to use for recovery without
that opt-in because its hardware and embedded tags are checked before OTBR is
stopped. Set `allow_legacy_rcp: false` after a tagged firmware has verified.

`allow_prereleases` is disabled by default. When enabled, the automatic policy
can select Nordic's current `vX.Y.Z-previewN` and `vX.Y.Z-rcN` releases in
addition to stable releases; arbitrary development tags are not releases. Set
`pinned_ncs_minor` to a major.minor line such as `3.4` to select only `3.4.x`
stable releases, or matching previews and RCs when `allow_prereleases: true`.

Changing either setting never flashes the radio by itself. On the next explicit
native `update.install`, a lower configured policy target is intentionally
treated as an approved downgrade when the currently installed HW/NCS/Zephyr
identity was previously verified. If the installed identity is unknown, the
native path fails closed instead; choose the exact release from the sidebar.
This means disabling previews after a newer preview, or pinning a lower minor
line after moving higher, can deliberately request the matching lower release.
The stock Secure DFU bootloader still enforces its own application-version
anti-rollback policy and may reject the transfer. A sidebar release selection
also cannot bypass that bootloader policy. URL and uploaded ELFs are accepted
only after the manual-validation rules described above, not as signed project
releases.

## Firmware Releases

The local `ncs_proj` checkout is NCS 3.3.0 and is intentionally rejected for a
3.3.4 release. Build from a complete checkout of the exact release instead:

```sh
python3 tools/build_rcp.py \
  --ncs-root /path/to/ncs-v3.3.4 \
  --expected-ncs-version 3.3.4 \
  --output-dir out/3.3.4
```

The command produces Zephyr's `zephyr.elf` output as the versioned firmware
file `nrf52840-ot-rcp-ncs-<version>.elf`, plus `release-metadata.json`. The
metadata contains the SHA-256, monotonic Secure DFU application version, and
the exact compiled `SPINEL_PROP_NCP_VERSION` string with its byte count. The
repository workflow consumes both files when it publishes the firmware branch.

`.github/workflows/ncs-candidate.yml` runs hourly. It finds NCS tags at or
above the baseline in `firmware/release-policy.json`: stable `vX.Y.Z` tags plus
the supported `vX.Y.Z-previewN` and `vX.Y.Z-rcN` prereleases. Each is built on
a standard GitHub Ubuntu runner using NCS sources, `west`, NCS Python
requirements, and the release-matched Zephyr SDK. The workflow publishes each
versioned ELF, detached signature, signed manifest entry, NCS license, SPDX
SBOM, generated notice, and provenance record to the dedicated `firmware`
branch. Its build jobs have read-only repository access and do not receive a
write-capable token; only the publication job requests `contents: write` for
the firmware branch. The
add-on pins the Ed25519 public key used to verify the manifest and every ELF,
so a mutable branch and SHA-256 alone cannot authorize firmware. The workflow
fails closed if NCS's exact license hash or the SBOM's concluded file licenses
are not approved. All GitHub actions are pinned to reviewed commit SHAs. It
creates no GitHub Release: GitHub always adds source archive links to Releases,
and those archives are irrelevant for an RCP firmware updater. The branch
contains only the signed index, firmware ELFs, signatures, and release evidence.
Stable, preview, and RC entries are selected from the same policy; there is no
review or promotion gate. GitHub does not provide a cross-repository release
trigger, so hourly polling is the automatic trigger.

The manifest and generated binaries are not committed to the source branch. If
the firmware branch does not exist yet, the workflow initializes it after the
first verified firmware build.

### Firmware Signing Setup

The active Ed25519 verifier is compiled into the add-on at
`app/firmware_signing_public_key.pem`. Its DER SHA-256 fingerprint is
`03715e0d5084c77c230119639fc46f5e225ff6722cd53e171ec266ffff1b94ca`.
The `0.4.1` bridge release also contains
`app/firmware_signing_legacy_public_key.pem`, fingerprint
`6048da9611bedad11db1b43743a41abf59f64140f23eab979f45bd6da52f8aea`, so it
can verify existing branch content before old releases are cleared and rebuilt
with the active key. The matching active private key must never be committed
or printed in Actions logs. Store its base64-encoded PEM as the
`FIRMWARE_SIGNING_PRIVATE_KEY_B64` secret of the `firmware-publisher` GitHub
environment. That environment needs no reviewer for automatic publication,
but should be restricted to the protected `main` branch. Protect `main` with
pull-request review and CI. On `firmware`, block force pushes and deletion, but
allow ordinary fast-forward updates: the publication job uses a `GITHUB_TOKEN`
push and would otherwise be blocked. The signed manifest and ELF verification,
not branch mutability, is the firmware integrity boundary.

Before setting that secret, verify that the private PEM is the one matching the
committed public key. This command must print the fingerprint above; it does
not print the private key:

```sh
openssl pkey -in /secure/path/firmware-signing-private-key.pem \
  -pubout -outform DER | sha256sum
```

Encode that existing unencrypted Ed25519 PEM as one standard RFC 4648 Base64
line, then paste the resulting contents into the environment secret. Do not
paste the raw `-----BEGIN PRIVATE KEY-----` PEM, surrounding quotes, an
`ed25519:` signature value, or URL-safe Base64.

```sh
base64 --wrap=0 /secure/path/firmware-signing-private-key.pem \
  > /secure/path/firmware-signing-private-key.pem.b64
```

On systems whose `base64` does not support `--wrap`, use this equivalent:

```sh
base64 < /secure/path/firmware-signing-private-key.pem | tr -d '\n' \
  > /secure/path/firmware-signing-private-key.pem.b64
```

Keep both the original private PEM and its Base64 copy outside the repository,
and delete the temporary Base64 copy after entering the GitHub secret.

### Rotate Firmware Signing Key

Use the local helper to create a replacement unencrypted Ed25519 keypair and
its GitHub-secret value. It refuses output within this repository, requires a
private output directory that is not accessible to group or other users, uses
mode `0600` for secret files, and self-tests the key with this project's actual
signature format. The helper does not change the add-on verifier or GitHub
secret itself.

Run it from the repository root, not its parent directory. Create the virtual
environment first if `.venv/bin/python` does not exist:

```sh
cd /home/user/proj/ha-nrf52840-ot-rcp-updater
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

KEY_DIR="$HOME/.local/share/ha-nrf52840-ot-rcp-updater/key-rotation-2026-09"
.venv/bin/python tools/rotate_firmware_signing_key.py generate \
  --output-dir "$KEY_DIR" \
  --key-id firmware-signing-2026-09
```

If you generated the unencrypted Ed25519 PEM independently, use `prepare`
instead. It validates the existing file and writes only the derived public PEM
and GitHub-secret Base64 value; it does not make another private-key copy.

```sh
.venv/bin/python tools/rotate_firmware_signing_key.py prepare \
  --private-key "$NEW_PRIVATE" \
  --output-dir "$KEY_DIR" \
  --key-id firmware-signing-2026-09
```

Back up the generated private PEM in KeePassXC. Do not share it, commit it, or
paste it into an issue. Share only the generated public PEM and its printed
DER SHA-256 fingerprint. Complete a rotation in this order:

1. Release and install an add-on bridge version that trusts both the old and
   replacement public keys. This repository's `0.4.1` release is that bridge
   for the active fingerprint above.
2. Confirm Home Assistant is running that bridge version. Do not replace the
   GitHub secret earlier: older add-ons trust only the legacy key and would
   reject newly signed manifests.
3. Paste the single-line contents of the generated `*-github-secret.b64` file
   into the `FIRMWARE_SIGNING_PRIVATE_KEY_B64` secret in the
   `firmware-publisher` GitHub environment.
4. In GitHub Actions, select **Publish NCS RCP Releases**, choose **Run
   workflow**, leave `ncs_tag` empty, and enable `reset_existing_firmware`.
   The job verifies the existing manifest with either trusted key, removes the
   old published release files, and writes an empty manifest signed by the
   active key.
5. Run the workflow a second time with both inputs empty, or wait for its next
   hourly run. The empty manifest makes the builder rebuild every eligible NCS
   release from source and publish it with the active key. Retire the legacy
   public key only in a later add-on release, after bridge users have had time
   to update.

If the generated Base64 file was deleted after a KeePassXC backup, recreate it
from the backed-up private PEM with the `prepare` command above. Never create
or paste a new private key merely to recreate the Base64 value.

When NCS changes its license text, update the reviewed
`ncs_license_sha256` value in `firmware/release-policy.json` in the same pull
request as the license review. The unattended workflow intentionally stops
until that explicit approval exists.

## Development Checks

```sh
.venv/bin/ruff check .
python3 -m unittest discover -s tests -v
python3 -m compileall -q nrf52840_ot_rcp_updater/app tools tests
```
