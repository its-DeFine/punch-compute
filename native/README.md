# Native source pins

`sources.json` records the repository, immutable upstream commit, complete Punch patch digest and resulting Git tree for each native dependency. These patches include the previously accepted native changes plus the retained strict accounting/recovery changes; they are not a new implementation.

To reconstruct a dependency in an isolated checkout:

1. Clone its recorded repository and check out `base_commit` detached.
2. Verify the corresponding patch SHA-256 against `patch_sha256`.
3. Apply `git apply --index /absolute/path/to/native/NAME.patch`.
4. Verify `git write-tree` equals `source_tree` before building.

Both patches were reconstructed against their recorded bases and produced the exact recorded source trees. No upstream source, license or attribution is removed. This source reconstruction is not a fresh GPU/payment acceptance or a claim of reproducible binary bytes. Build with that dependency's pinned toolchain and existing build instructions. The image Dockerfiles in `packaging/` consume the resulting native binaries; do not replace those with unpinned upstream binaries.

Do not push the New API changes to QuantumNous/new-api. This repository owns the product patches and release source. Existing accepted image digests remain in Compose; source consolidation alone does not require rebuilding them.
