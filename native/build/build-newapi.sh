#!/bin/sh
set -eu
cd /src
export GO111MODULE=on CGO_ENABLED=0 GOWORK=off GOOS=linux GOARCH=amd64
export GOEXPERIMENT=greenteagc GOMAXPROCS=2 GOMEMLIMIT=800MiB
export GOPATH=/cache/gopath GOCACHE=/cache/build GOFLAGS='-mod=readonly -p=1'
test -s web/dist/index.html
go version
go mod download
go mod verify
go build -buildvcs=false -trimpath -ldflags "-s -w -X github.com/QuantumNous/new-api/common.Version=punch-74fb7c8f3e34-rebuilt-20261021" -o /out/new-api-strict1
sha256sum /out/new-api-strict1
go version -m /out/new-api-strict1
