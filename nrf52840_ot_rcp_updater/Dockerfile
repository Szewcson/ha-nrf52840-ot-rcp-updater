ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
ARG NRFDFU_REVISION=8a8e0adda6b0f44cf1ba9aee62c62a67df335137

FROM rust:1.82-bookworm AS nrfdfu-builder

ARG NRFDFU_REVISION

COPY patches/nrfdfu-cli-init-packet-versions.patch \
     patches/nrfdfu-cli-exact-port.patch /patches/

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        git \
    && git clone https://github.com/knurling-rs/nrfdfu-rs.git /src/nrfdfu-rs \
    && git -C /src/nrfdfu-rs checkout --detach "${NRFDFU_REVISION}" \
    && test "$(git -C /src/nrfdfu-rs rev-parse HEAD)" = "${NRFDFU_REVISION}" \
    && git -C /src/nrfdfu-rs apply --check --unidiff-zero /patches/nrfdfu-cli-init-packet-versions.patch \
    && git -C /src/nrfdfu-rs apply --unidiff-zero /patches/nrfdfu-cli-init-packet-versions.patch \
    && git -C /src/nrfdfu-rs apply --check --unidiff-zero /patches/nrfdfu-cli-exact-port.patch \
    && git -C /src/nrfdfu-rs apply --unidiff-zero /patches/nrfdfu-cli-exact-port.patch \
    && cargo build --release --locked --manifest-path /src/nrfdfu-rs/Cargo.toml \
    && rm -rf /var/lib/apt/lists/*

FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        libgcc-s1 \
        libstdc++6 \
        python3 \
        python3-paho-mqtt \
        python3-serial \
    && rm -rf /var/lib/apt/lists/*

COPY --from=nrfdfu-builder /src/nrfdfu-rs/target/release/nrfdfu /usr/local/bin/nrfdfu
COPY --from=nrfdfu-builder /src/nrfdfu-rs/LICENSE-APACHE /usr/share/licenses/nrfdfu-rs/LICENSE-APACHE
COPY LICENSES/nrfdfu-rs-NOTICE.md /usr/share/licenses/nrfdfu-rs/NOTICE.md

COPY app /app
COPY run.sh /run.sh
RUN chmod 0755 /usr/local/bin/nrfdfu /run.sh

CMD ["/run.sh"]
