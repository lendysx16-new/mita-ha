# Build Mita 3.40.0 from source with the per-account outbound patch (mita/upstream).
FROM golang:1.25-bookworm AS mita-routed-builder
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl patch protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
# The Go module proxy occasionally drops downloads mid-stream, and GOPROXY's
# "direct" fallback only applies to 404/410. Retry, and fetch every module in its
# own cached layer so the patch/test/build step below needs no network.
RUN for attempt in 1 2 3 4 5; do \
      go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2 && exit 0; \
      echo "protoc-gen-go install failed (attempt ${attempt}), retrying" >&2; sleep $((attempt * 5)); \
    done; exit 1
RUN curl -fL --retry 3 -o /tmp/mieru.tar.gz \
      https://codeload.github.com/enfein/mieru/tar.gz/9812207bb8aefc3dd4dcc6a5ce268bf5e28b6c54 \
    && tar -xzf /tmp/mieru.tar.gz --strip-components=1 \
    && rm /tmp/mieru.tar.gz
RUN for attempt in 1 2 3 4 5; do \
      go mod download && go mod verify && exit 0; \
      echo "go mod download failed (attempt ${attempt}), retrying" >&2; sleep $((attempt * 5)); \
    done; exit 1
COPY upstream/per-user-outbound.patch /tmp/per-user-outbound.patch
COPY upstream/user_outbound.go upstream/user_outbound_test.go pkg/socks5/
RUN patch --batch --fuzz=0 -p1 < /tmp/per-user-outbound.patch \
    && protoc -I pkg --go_out=pkg/appctl --go_opt=module=github.com/enfein/mieru/v3/pkg/appctl \
         pkg/appctl/proto/base.proto \
    && go test ./pkg/socks5 -run TestPerUserProxyOutbound \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mita ./cmd/mita

FROM debian:bookworm-slim AS awgm-box-downloader
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) asset="linux-amd64"; checksum="d8438f3ed030a5b964b23175c870b3749ecff9b844f48fe96723cfcd11e16f8d" ;; \
      arm64) asset="aarch64-3.10"; checksum="e286e0704ac4daf9b0039f0fce5d6b9fc0dcf1b0ad5092615ee037eeac580dfd" ;; \
      *) echo "Unsupported awgm-box architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fL --retry 3 -o /tmp/awgm-box \
       "https://github.com/hoaxisr/awgm-box/releases/download/42/awgm-box-42-${asset}" \
    && echo "${checksum}  /tmp/awgm-box" | sha256sum -c - \
    && install -m 0755 /tmp/awgm-box /usr/local/bin/awgm-box

FROM debian:bookworm-slim AS awgm-box-runtime
COPY --from=awgm-box-downloader /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=awgm-box-downloader /usr/local/bin/awgm-box /usr/local/bin/awgm-box
ENTRYPOINT ["/usr/local/bin/awgm-box"]

FROM debian:bookworm-slim AS mita-runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mita-routed-builder /out/mita /usr/bin/mita
COPY daemon.py /app/daemon.py
RUN chmod 0755 /usr/bin/mita /app/daemon.py
CMD ["python3", "/app/daemon.py"]

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates python3 python3-psycopg2 python3-yaml \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mita-routed-builder /out/mita /usr/bin/mita
COPY --from=awgm-box-downloader /usr/local/bin/awgm-box /usr/local/bin/awgm-box
COPY run-backend.sh /run-backend.sh
COPY webapp.py /app/webapp.py
COPY subscription.html /app/subscription.html
COPY admin.html /app/admin.html
COPY clients.json /app/clients.json

RUN chmod 0755 /usr/bin/mita /run-backend.sh /app/webapp.py \
    && chmod 0644 /app/subscription.html /app/admin.html /app/clients.json

HEALTHCHECK --interval=5s --timeout=2s --start-period=30s --retries=12 \
    CMD python3 -c 'import socket; socket.create_connection(("127.0.0.1", 8098), timeout=2).close()' || exit 1
CMD ["/run-backend.sh"]
