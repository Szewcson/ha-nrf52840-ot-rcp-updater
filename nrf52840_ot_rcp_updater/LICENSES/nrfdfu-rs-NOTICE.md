# nrfdfu-rs Notice

The add-on image contains a locally patched `nrfdfu` binary built from
[`knurling-rs/nrfdfu-rs`](https://github.com/knurling-rs/nrfdfu-rs) commit
`8a8e0adda6b0f44cf1ba9aee62c62a67df335137`.

Upstream licenses `nrfdfu-rs` under `MIT OR Apache-2.0`. This project chooses
the Apache-2.0 option for the modified binary; the complete Apache-2.0 license
is included in the add-on image at `/usr/share/licenses/nrfdfu-rs/LICENSE-APACHE`.
The local source changes are limited to two independent patches:

- `patches/nrfdfu-cli-init-packet-versions.patch` exposes the Secure DFU
  firmware and hardware version fields as CLI options.
- `patches/nrfdfu-cli-exact-port.patch` accepts one exact serial endpoint.
