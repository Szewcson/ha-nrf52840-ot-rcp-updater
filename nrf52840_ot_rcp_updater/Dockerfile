ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
ARG NRFDFU_REVISION=8a8e0adda6b0f44cf1ba9aee62c62a67df335137
ARG RUST_BUILDER_IMAGE=rust:1.82-bookworm@sha256:d9c3c6f1264a547d84560e06ffd79ed7a799ce0bff0980b26cf10d29af888377

FROM ${RUST_BUILDER_IMAGE} AS nrfdfu-builder

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
    && cargo metadata --locked --format-version 1 --manifest-path /src/nrfdfu-rs/Cargo.toml \
        > /src/nrfdfu-rs/dependency-metadata.json \
    && rm -rf /var/lib/apt/lists/*

FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        libgcc-s1 \
        libstdc++6 \
        python3 \
        python3-cryptography \
        python3-paho-mqtt \
        python3-serial \
    && install -d -m 0755 /usr/share/licenses/pca10059-openthread-rcp-updater \
    && dpkg-query -W | sort \
        > /usr/share/licenses/pca10059-openthread-rcp-updater/debian-packages.tsv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=nrfdfu-builder /src/nrfdfu-rs/target/release/nrfdfu /usr/local/bin/nrfdfu
COPY --from=nrfdfu-builder /src/nrfdfu-rs/LICENSE-APACHE /usr/share/licenses/nrfdfu-rs/LICENSE-APACHE
COPY --from=nrfdfu-builder /src/nrfdfu-rs/dependency-metadata.json /usr/share/licenses/nrfdfu-rs/dependency-metadata.json
COPY LICENSES/nrfdfu-rs-NOTICE.md /usr/share/licenses/nrfdfu-rs/NOTICE.md
COPY LICENSES/Apache-2.0.txt /usr/share/licenses/pca10059-openthread-rcp-updater/LICENSE
COPY LICENSES/COMPONENTS.md /usr/share/licenses/pca10059-openthread-rcp-updater/COMPONENTS.md

COPY app /app
COPY run.sh /run.sh
RUN chmod 0755 /usr/local/bin/nrfdfu /run.sh

CMD ["/run.sh"]
