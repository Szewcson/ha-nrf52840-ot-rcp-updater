# nRF52840 OpenThread RCP Updater

Home Assistant app that exposes a native `update` entity for an nRF52840
Dongle (PCA10059) used as the RCP for the official OpenThread Border Router
app. It never replaces OTBR: the app performs one controlled hand-off from
OTBR to Nordic DFU and back.

## Version Contract

`SPINEL_PROP_NCP_VERSION` already returns OpenThread's standard string:

```text
PACKAGE_NAME/PACKAGE_VERSION; OPENTHREAD_CONFIG_PLATFORM_INFO; BUILD_DATETIME
```

This project changes only `OPENTHREAD_CONFIG_PLATFORM_INFO` during the RCP
build. The required value is:

```text
HW/PCA10059 NCS/<ncs-version> ZEPHYR/<zephyr-version>
```

No custom Spinel property is introduced. No `FW/...` tag is added: the
existing OpenThread package version and build timestamp are retained.

The Home Assistant update entity compares the reported `NCS` field with the
newest matching entry in `firmware/manifest.json`. Hardware and Zephyr fields
must also match after flashing.

## Update Flow

1. Home Assistant discovers `nRF52840 OT RCP` as an MQTT `update` entity.
2. An explicit `update.install` request downloads a HTTPS artifact and verifies
   its SHA-256 and Nordic DFU ZIP structure before OTBR is stopped.
3. With `safe_update: true`, the app checks OTBR REST management actions twice
   over the configured quiet window. This detects commissioning or diagnostic
   work, but cannot prove that all Thread application traffic is idle.
4. The app stops OTBR through the Supervisor API, exclusively opens the Spinel
   device, asks for `SPINEL_RESET_BOOTLOADER`, and programs only the configured
   Nordic USB DFU serial number with `nrfutil device program`.
5. It verifies `HW`, `NCS`, and `ZEPHYR` through `SPINEL_PROP_NCP_VERSION`,
   restarts OTBR in a `finally` block, waits for its REST health endpoint, and
   records state only after successful verification.

The update entity is compatible with Home Assistant automations. This project
does not run a hidden scheduler in the add-on; an automation can call
`update.install` at a maintenance window after you decide the traffic policy.

## Configuration

Use stable `/dev/serial/by-id/...` paths when configuring the app.

```yaml
device: /dev/serial/by-id/usb-Nordic_Semiconductor_Thread_Co-Processor-if00
baudrate: 1000000
hardware: PCA10059
otbr_addon_slug: core_openthread_border_router
otbr_api_url: http://YOUR_OTBR_HOST:8081
safe_update: true
allow_legacy_rcp: false
dfu_serial_number: A1234B5678C9
manifest_url: https://raw.githubusercontent.com/OWNER/REPOSITORY/main/firmware/manifest.json
manifest_poll_interval: 3600
idle_window: 20
boot_timeout: 45
```

`dfu_serial_number` is the Nordic DFU USB identity, not the tty path. Find it
with `nrfutil device list --traits nordicDfu`. The add-on requests USB access
because the nRF52840 Dongle's built-in bootloader is a USB DFU device.

The app reloads `manifest_url` every hour by default and updates the native HA
entity when a newer NCS release is published. To install automatically, create
a Home Assistant automation for that entity which calls `update.install` in a
maintenance window; the actual RCP comparison still happens through Spinel
immediately before flashing.

Keep `allow_legacy_rcp` false. A legacy RCP without the three tags requires an
explicit opt-in because its hardware cannot be verified before the flash.

## Firmware Releases

The local `ncs_proj` checkout is NCS 3.3.0 and is intentionally rejected for a
3.3.4 release. Build from a complete checkout of the exact release instead:

```sh
python3 tools/build_rcp.py \
  --ncs-root /path/to/ncs-v3.3.4 \
  --expected-ncs-version 3.3.4 \
  --output-dir out/3.3.4
```

The command produces a Nordic DFU ZIP plus `release-metadata.json`. The
repository workflow consumes those two files when it creates a release.

`.github/workflows/ncs-candidate.yml` runs hourly. It lists every stable
`sdk-nrf` release at or above the baseline in `firmware/release-policy.json`,
builds any that are absent from the manifest with Nordic's version-matched
toolchain container, creates a GitHub release, and commits the SHA-256 manifest
entry automatically. There is no review or promotion gate. GitHub does not
provide a cross-repository release trigger, so hourly polling is the automatic
trigger here.

## Development Checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q nrf52840_ot_rcp_updater/app tools tests
```
