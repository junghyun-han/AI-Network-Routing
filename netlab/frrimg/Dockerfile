# Native FRRouting image (works on arm64 and amd64 alike, unlike the
# amd64-only official image which segfaults under emulation on Apple Silicon).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      frr frr-pythontools iproute2 iputils-ping tini \
 && rm -rf /var/lib/apt/lists/*

COPY start.sh /start.sh
RUN chmod +x /start.sh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/start.sh"]
