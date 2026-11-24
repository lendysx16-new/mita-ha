ARG BUILD_ARCH
ARG MITA_BASE_IMAGE=ghcr.io/home-assistant/base:latest
FROM debian:bookworm-slim AS downloader

ARG BUILD_ARCH
ARG TARGETARCH
ARG MITA_VERSION=3.40.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && case "${BUILD_ARCH:-${TARGETARCH}}" in \
         amd64) DEB_ARCH="amd64" ;; \
         aarch64|arm64) DEB_ARCH="arm64" ;; \
         *) echo "Unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fL --retry 3 \
       -o /tmp/mita.deb \
       "https://github.com/enfein/mieru/releases/download/v${MITA_VERSION}/mita_${MITA_VERSION}_${DEB_ARCH}.deb" \
    && dpkg-deb -x /tmp/mita.deb /tmp/mita-root

# Build the routed Linux variant against the exact Mita 3.40.0 source.
FROM golang:1.25-bookworm AS mita-routed-builder
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl patch protobuf-compiler \
    && rm -rf /var/lib/apt/lists/* \
    && go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2
RUN curl -fL --retry 3 -o /tmp/mieru.tar.gz \
      https://codeload.github.com/enfein/mieru/tar.gz/9812207bb8aefc3dd4dcc6a5ce268bf5e28b6c54 \
    && tar -xzf /tmp/mieru.tar.gz --strip-components=1 \
    && rm /tmp/mieru.tar.gz
COPY upstream/per-user-outbound.patch /tmp/per-user-outbound.patch
COPY upstream/user_outbound.go upstream/user_outbound_test.go pkg/socks5/
RUN patch --batch --fuzz=0 -p1 < /tmp/per-user-outbound.patch \
    && protoc -I pkg --go_out=pkg/appctl --go_opt=module=github.com/enfein/mieru/v3/pkg/appctl \
         pkg/appctl/proto/base.proto \
    && go test ./pkg/socks5 -run TestPerUserProxyOutbound \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mita ./cmd/mita

FROM ${MITA_BASE_IMAGE} AS mita-runtime

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN if command -v apk >/dev/null; then \
      apk add --no-cache python3 py3-psycopg2; \
    else \
      apt-get update \
      && apt-get install -y --no-install-recommends bash ca-certificates python3 python3-psycopg2 \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=downloader /tmp/mita-root/usr/bin/mita /usr/bin/mita
COPY run.sh /run.sh
COPY webapp.py /app/webapp.py
COPY subscription.html /app/subscription.html
COPY admin.html /app/admin.html
COPY clients.json /app/clients.json

RUN chmod 0755 /usr/bin/mita /run.sh /app/webapp.py \
    && chmod 0644 /app/subscription.html /app/admin.html /app/clients.json

CMD ["/run.sh"]

FROM ghcr.io/sagernet/sing-box:v1.14.4 AS sing-box

FROM mita-runtime AS linux
COPY --from=mita-routed-builder /out/mita /usr/bin/mita
COPY --from=sing-box /usr/local/bin/sing-box /usr/local/bin/sing-box

FROM mita-runtime AS home-assistant
