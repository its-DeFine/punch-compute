#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends build-essential pkg-config autoconf git curl ca-certificates python3 nasm xz-utils
mkdir -p /work/toolchain /work/ffmpeg-deps /work/cache /out
if [ ! -x /work/toolchain/go/bin/go ]; then
  curl -fsSL --retry 3 --connect-timeout 15 https://go.dev/dl/go1.25.0.linux-amd64.tar.gz -o /work/toolchain/go.tar.gz
  printf '%s  %s\n' 2852af0cb20a13139b3448992e69b868e50ed0f8a1e5940ee1de9e19a123b613 /work/toolchain/go.tar.gz | sha256sum -c -
  tar -C /work/toolchain -xzf /work/toolchain/go.tar.gz
fi
export PATH="/work/toolchain/go/bin:$PATH"
export NPROC=2 GOOS=linux GOARCH=amd64 GOMAXPROCS=2 GOMEMLIMIT=800MiB
export GOPATH=/work/gopath GOCACHE=/work/cache GOFLAGS='-mod=readonly -p=1'
export BUILD_TAGS=mainnet,experimental
bash /inputs/lpms-install_ffmpeg.sh /work/ffmpeg-deps
export PKG_CONFIG_PATH=/work/ffmpeg-deps/compiled/lib/pkgconfig
cd /src
go version
go mod download
go mod verify
CGO_ENABLED=1 go build -buildvcs=false -trimpath -tags "$BUILD_TAGS" -ldflags '-X github.com/livepeer/go-livepeer/core.LivepeerVersion=0.9.2-punch-fccfea64968c-rebuilt-20261021' -o /out/livepeer ./cmd/livepeer
sha256sum /out/livepeer
ldd /out/livepeer
