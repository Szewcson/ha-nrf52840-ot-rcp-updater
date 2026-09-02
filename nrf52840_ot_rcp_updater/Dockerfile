ARG BUILD_FROM
FROM $BUILD_FROM

ARG BUILD_ARCH
ARG NRFUTIL_DEVICE_VERSION=2.20.1

RUN apk add --no-cache curl gcompat libgcc libstdc++ python3 py3-paho-mqtt py3-pyserial \
    && case "${BUILD_ARCH}" in \
        amd64) \
            nrfutil_url="https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil"; \
            nrfutil_sha256="4800f8b71feec0caaf3c143d29b0b7fd6f33c6a258da10e660743f0db1890a0b" ;; \
        aarch64) \
            nrfutil_url="https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/aarch64-unknown-linux-gnu/nrfutil"; \
            nrfutil_sha256="0a1d87ad5eab2292beb847462c69b3b9db37aba3a59bbd56e31de7ce45b12a9a" ;; \
        *) echo "Unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error "${nrfutil_url}" --output /usr/local/bin/nrfutil \
    && echo "${nrfutil_sha256}  /usr/local/bin/nrfutil" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/nrfutil \
    && nrfutil install "device=${NRFUTIL_DEVICE_VERSION}" --force

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
