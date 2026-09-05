# nRF52840 OpenThread RCP Updater

Home Assistant app that exposes a native `update` entity for an nRF52840
Dongle (PCA10059) used as the RCP for the official OpenThread Border Router
app. It never replaces OTBR: the app performs one controlled hand-off from
OTBR to the stock PCA10059 Secure DFU bootloader and back.

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

1. Home Assistant discovers `nRF52840 OT RCP` as an MQTT `update` entity.
2. An explicit `update.install` request downloads a HTTPS artifact and verifies
   its SHA-256 plus its 32-bit little-endian ARM ELF headers and PCA10059 flash
   segments before OTBR is stopped.
3. With `safe_update: true`, the app checks OTBR REST management actions twice
   over the configured quiet window when OTBR is running. This detects
   commissioning or diagnostic work, but cannot prove that all Thread
   application traffic is idle. If OTBR was explicitly stopped before the
   request, it is treated as an offline maintenance window: REST checks are
   omitted and OTBR is left stopped after the update.
4. The app stops OTBR through the Supervisor API, records the RCP's physical
   USB topology, exclusively opens the Spinel device, asks for
   `SPINEL_RESET_BOOTLOADER`, waits for Nordic `1915:521f` on that same physical
   USB port, and programs its ELF with `nrfdfu --serial --port --fw-version --abort`.
   The bootloader descriptor serial is discovered internally only after the
   topology check and the command returns the dongle to the RCP application.
5. It verifies `HW`, `NCS`, and `ZEPHYR` through `SPINEL_PROP_NCP_VERSION`,
   restarts OTBR in a `finally` block, waits for its REST health endpoint, and
   records state only after successful verification. A failed install clears
   the recorded version, so Home Assistant reports `unknown` rather than a
   potentially stale firmware version.

The update entity reports retained `0-100%` stage progress throughout this
flow, and clears it when the operation completes or fails.

MQTT control messages are deliberately one-shot. The updater ignores retained
commands and QoS redeliveries marked as duplicates, so a broker replay cannot
repeat a firmware operation after an app restart or reconnect. A recovery
attempt must come from a new explicit Home Assistant action.

The update entity is compatible with Home Assistant automations. This project
does not run a hidden scheduler in the add-on; an automation can call
`update.install` at a maintenance window after you decide the traffic policy.

## Runtime Image

The app continues to use Home Assistant's Debian Bookworm base image. The
removal of the Nordic runtime binary makes a future move to the standard Alpine
base possible, but that independent base-image migration is intentionally not
part of this change.

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

With the profile active, the current Supervisor score is expected to be `4`:
the base `5`, plus one for AppArmor, minus one for the required `manager` role,
and minus one for the required host network. A score of `3` means the custom
profile is not active on that Home Assistant host. The app intentionally does
not switch to a broader Home Assistant API role or add a no-op ingress UI merely
to raise the number.

The official OpenThread Border Router app uses Home Assistant's host network.
This app does too, so its safe-update preflight can reach OTBR at
`http://127.0.0.1:8081`. In the OTBR app configuration, use **Show disabled
ports** to enable the OpenThread REST API on port `8081`. Do not use
`core-openthread-border-router` as `otbr_api_url`: Home Assistant does not
make host-network apps addressable by generated DNS names from ordinary app
networks.

If OTBR reports `started` or `running`, an unreachable REST API is a safety
failure and the update is rejected. To update while the REST API is
unavailable, explicitly stop the OTBR app first, wait for its state to become
`stopped`, then request the update. The updater will not restart an app that
was already stopped; start OTBR manually after the RCP has been verified.

## Runtime Tool Licensing

This repository's source code is licensed under Apache-2.0. The RCP firmware is
built from the nRF Connect SDK (NCS) for an nRF52840, so its distribution also
remains subject to the [NCS license](https://github.com/nrfconnect/sdk-nrf/blob/main/LICENSE)
and the notices of its transitive modules.

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
set.

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

`baudrate` is a fixed Home Assistant selector matching the official OTBR
frontend choices: `57600`, `115200`, `230400`, `460800`, `921600`, and
`1000000`. The PCA10059 build uses USB CDC ACM, so this app intentionally does
not expose a hardware-flow-control setting: Nordic's flow-control guidance is
for UART-connected development kits, not this USB dongle. Use the same baudrate
as OTBR; current project firmware uses `1000000` by default.

This app intentionally supports one target: a PCA10059 RCP, its stock Nordic
Secure DFU bootloader (`1915:521f`), and this repository's verified firmware
manifest. It also supports only Home Assistant's official
`core_openthread_border_router` app, using its local REST API at
`http://127.0.0.1:8081` for the quiet-window and post-flash health checks.
These are not configuration choices. On first startup after this upgrade, the
app removes the retired `hardware`, `manifest_url`, `otbr_addon_slug`,
`otbr_api_url`, and `dfu_vid_pid` options through the Supervisor API.

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
`device` value unchanged. Choose a **Manual RCP firmware target** before
pressing **Flash selected RCP firmware**, or enable `allow_legacy_rcp` to
recover an untagged RCP with the newest release allowed by the selected channel
and minor pin. The updater accepts exactly one `1915:521f` bootloader, skips
the Spinel reset, flashes the verified ELF, and still requires the normal
post-flash Spinel version check. Set `dfu_serial_number` only if several
bootloaders match with distinct serials, or `dfu_usb_path` if their serials are
identical. If the exact DFU target remains present after that check
times out, the updater sends one additional Secure DFU application reboot and
performs one final verification window; it never flashes the image twice.

The app also creates a **Manual RCP firmware target** selector in Home
Assistant. Its
list is refreshed from the verified manifest and contains `Automatic` plus the
eligible stable releases, or preview/RC releases when `allow_prereleases` is
enabled. Select one version and press **Flash selected RCP firmware** to
request that explicit manual flash, including a controlled downgrade. This
button is separate because Home Assistant correctly marks an older selected
version as up to date on an Update entity and therefore hides Install. The
button rejects requests until a manual target has been selected. The selection
resets to `Automatic` only after post-flash Spinel verification. The verified
`HW`, `NCS`, and `ZEPHYR` tags are then persisted, and the update entity returns
to its normal automatic release policy. The update entity's
diagnostic attributes show the configured device path, detected normal-RCP USB
serial, configured DFU VID:PID, whether the normal USB topology is known, the
optional and resolved DFU serials, DFU presence, and the official OTBR app slug.

The Update entity always reports `installed_version` from the last verified
Spinel response and `latest_version` from the automatic release policy. After a
manual downgrade, the card can therefore show a newer release as **Available
firmware**; it is not the firmware currently running on the RCP. The card's
release summary and attributes also identify the installed and available NCS
versions explicitly.

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
explicit opt-in because its hardware cannot be verified before the flash. With
`allow_legacy_rcp: true`, it uses the newest release from the selected stable
or prerelease channel and optional minor pin, unless a Manual RCP firmware
target was selected. After the flashed RCP reports its `HW`, `NCS`, and
`ZEPHYR` tags, set `allow_legacy_rcp: false`.

`allow_prereleases` is disabled by default. When enabled, the update entity may
offer Nordic's current `vX.Y.Z-previewN` and `vX.Y.Z-rcN` builds in addition to
stable releases. The project does not treat arbitrary development tags as
releases.

Set `pinned_ncs_minor` to a major.minor line such as `3.4` to receive only
`3.4.x` stable updates, or matching previews and RCs when
`allow_prereleases: true`. A pin never causes an automatic downgrade: if the
RCP already runs a newer line, the entity reports an error instead.

Manual release selection does not bypass the stock Secure DFU bootloader's
application-version anti-rollback check, so the bootloader can still reject a
lower firmware version. It also does not upload arbitrary local ELF files:
every manual target is downloaded from the verified manifest and checked
against its published SHA-256 before flashing.

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

`.github/workflows/ncs-candidate.yml` runs hourly. It finds Nordic tags at or
above the baseline in `firmware/release-policy.json`: stable `vX.Y.Z` tags plus
the supported `vX.Y.Z-previewN` and `vX.Y.Z-rcN` prereleases. Each is built on
a standard GitHub Ubuntu runner using NCS sources, `west`, NCS Python
requirements, and the release-matched Zephyr SDK. The workflow publishes each
versioned ELF and SHA-256 manifest entry to the dedicated `firmware` branch. Its
build jobs have read-only repository access and do not receive a GitHub token;
only the publication job can write the firmware branch. All GitHub actions are
pinned to reviewed commit SHAs. It
creates no GitHub Release: GitHub always adds source archive links to Releases,
and those archives are irrelevant for an RCP firmware updater. The branch
contains only `manifest.json` and versioned ELF files. Stable, preview, and RC
entries are selected from the same policy; there is no review or promotion gate.
GitHub does not provide a cross-repository release trigger, so hourly polling
is the automatic trigger.

The manifest and generated binaries are not committed to the source branch. If
the firmware branch does not exist yet, the workflow initializes it after the
first verified firmware build.

## Development Checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q nrf52840_ot_rcp_updater/app tools tests
```
