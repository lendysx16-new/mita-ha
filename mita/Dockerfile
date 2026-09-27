ARG BUILD_ARCH
FROM debian:bookworm-slim AS downloader

ARG BUILD_ARCH
ARG MITA_VERSION=3.38.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && case "${BUILD_ARCH}" in \
         amd64) DEB_ARCH="amd64" ;; \
         aarch64) DEB_ARCH="arm64" ;; \
         *) echo "Unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fL --retry 3 \
       -o /tmp/mita.deb \
       "https://github.com/enfein/mieru/releases/download/v${MITA_VERSION}/mita_${MITA_VERSION}_${DEB_ARCH}.deb" \
    && dpkg-deb -x /tmp/mita.deb /tmp/mita-root

FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

COPY --from=downloader /tmp/mita-root/usr/bin/mita /usr/bin/mita
COPY run.sh /run.sh
RUN chmod 0755 /usr/bin/mita /run.sh

CMD ["/run.sh"]
