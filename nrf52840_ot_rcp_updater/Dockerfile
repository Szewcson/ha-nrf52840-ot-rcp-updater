ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24
ARG NRFDFU_REVISION=8a8e0adda6b0f44cf1ba9aee62c62a67df335137
ARG RUST_BUILDER_IMAGE=rust:1.82-alpine@sha256:2f42ce0d00c0b14f7fd84453cdc93ff5efec5da7ce03ead6e0b41adb1fbe834e

FROM ${RUST_BUILDER_IMAGE} AS nrfdfu-builder

ARG NRFDFU_REVISION

COPY patches/nrfdfu-cli-init-packet-versions.patch \
     patches/nrfdfu-cli-exact-port.patch /patches/

RUN apk add --no-cache \
        build-base \
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
        > /src/nrfdfu-rs/dependency-metadata.json

FROM ${BUILD_FROM}

RUN apk add --no-cache \
        ca-certificates \
        libgcc \
        libstdc++ \
        python3 \
        py3-cryptography \
        py3-paho-mqtt \
        py3-pyserial \
    && install -d -m 0755 /usr/share/licenses/pca10059-openthread-rcp-updater \
    && apk query --from installed --format json --fields name,version "*" \
        | python3 -c 'import json, sys; sys.stdout.writelines("{name}\t{version}\n".format(**package) for package in sorted(json.load(sys.stdin), key=lambda package: package["name"]))' \
        > /usr/share/licenses/pca10059-openthread-rcp-updater/alpine-packages.tsv

COPY --from=nrfdfu-builder /src/nrfdfu-rs/target/release/nrfdfu /usr/local/bin/nrfdfu
COPY --from=nrfdfu-builder /src/nrfdfu-rs/LICENSE-APACHE /usr/share/licenses/nrfdfu-rs/LICENSE-APACHE
COPY --from=nrfdfu-builder /src/nrfdfu-rs/dependency-metadata.json /usr/share/licenses/nrfdfu-rs/dependency-metadata.json
COPY LICENSES/nrfdfu-rs-NOTICE.md /usr/share/licenses/nrfdfu-rs/NOTICE.md
COPY LICENSES/Apache-2.0.txt /usr/share/licenses/pca10059-openthread-rcp-updater/LICENSE
COPY LICENSES/COMPONENTS.md /usr/share/licenses/pca10059-openthread-rcp-updater/COMPONENTS.md

COPY app /app
COPY run.sh /run.sh
RUN chmod 0755 /usr/local/bin/nrfdfu /run.sh \
    && nrfdfu --help >/dev/null \
    && PYTHONPATH=/ python3 -c 'import cryptography, paho.mqtt.client, serial; import app.main'

CMD ["/run.sh"]
