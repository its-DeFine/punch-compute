# Native source pins

`sources.json` records the repository, immutable upstream commit, complete Punch patch digest and resulting Git tree for each native dependency. These patches include the previously accepted native changes plus the retained strict accounting/recovery changes; they are not a new implementation.

To reconstruct a dependency in an isolated checkout:

1. Clone its recorded repository and check out `base_commit` detached.
2. Verify the corresponding patch SHA-256 against `patch_sha256`.
3. Apply `git apply --index /absolute/path/to/native/NAME.patch`.
4. Verify `git write-tree` equals `source_tree` before building.

Both patches were reconstructed against their recorded bases and produced the exact recorded source trees. No upstream source, license or attribution is removed. This source reconstruction is not a fresh GPU/payment acceptance or a claim of reproducible binary bytes. Build with that dependency's pinned toolchain and existing build instructions. The image Dockerfiles in `packaging/` consume the resulting native binaries; do not replace those with unpinned upstream binaries.

Do not push the New API changes to QuantumNous/new-api. This repository owns the product patches and release source. The 2026-10-21 release rebuild uses these exact source trees. Its new image digests are recorded in Compose and the release build receipt; they are not claimed to match historical binary bytes.


## 2026-10-21 build procedure

`build/inputs.json` records the pinned builder images and source trees. These
are newly built Linux amd64 artifacts, not historical binary equivalents.
The build scripts consume reconstructed sources at `/src`, an owned cache at
`/cache` (New API) or `/work` (Livepeer), and write binaries to `/out`.
Run one build at a time with 2 CPUs and 2 GiB; keep source read-only for native
compilation. No GPU or chain connection is used by these builds.

For New API, first run the source Dockerfile's frontend steps using the pinned
Bun image: `bun install --frozen-lockfile` in `web/`, then
`DISABLE_ESLINT_PLUGIN=true VITE_REACT_APP_VERSION=$(cat ../VERSION) bun run build`.
The complete frontend is embedded by the native compile. Run
`sh /inputs/build-newapi.sh` in the pinned Go image with the source, cache,
output and this `build/` directory mounted at the paths above and `/inputs`.

For go-livepeer, run `bash /inputs/build-livepeer.sh` in the pinned Ubuntu
22.04 image with the source, work, output and build directory mounted as above.
This reuses the upstream CPU CI dependency installer and exact Go 1.25.0
checksum. It builds with `mainnet,experimental` tags and no CUDA/nonfree path.
The FFmpeg build enables GPL and libx264; preserve those sources and licenses
alongside the native executable. Container apt packages are resolved at build
time, so this recipe does not claim fully hermetic or bit-for-bit builds.

Package `new-api-strict1` and `livepeer` using the existing Dockerfiles in
`packaging/`, and the gateway from `app/` using `Dockerfile.order-api`.
Record the resulting content IDs in Compose and distribute the same images in
`images.tar.gz`; `images.sha256` is the archive's plain SHA-256 hex digest.
The release build receipt records the actual binary and image identities.

The opt-in native checks use `PUNCH_SETUP_NATIVE_BINARY`,
`PUNCH_NATIVE_CONFIG_IMAGE`, and `PUNCH_NATIVE_RUNNER_BINARY` in the existing
setup, native-config and runner-registration tests. Run the runner fixture in
`--network none`, mounting the source read-only, using UID/GID 1000 and a
writable owned temporary directory. It registers and proxies only a health
request; it does not exercise funded-chain or GPU inference behavior.
