# Provider setup and recovery

## Required artifacts and host

Use Linux UID/GID 1000, Python 3, Docker Compose supporting `!override`, NVIDIA
CDI and a suitable GPU. The exact New API, order API and Livepeer images are
pinned in `compose.yaml`; llama.cpp uses an upstream registry digest. This
checkout supplies no application image registry or downloadable image archive.
Obtain the verified runtime bundle before deployment. A freshly rebuilt image
or historical bundle with different content IDs is not a substitute.

A runtime directory contains `compose.yaml`, `compose.models.yaml`, `punch-stack`,
`diagnostics.py`, `setup.py`, and `bin/punch-token-orders`, plus `images.tar.gz`
and `images.sha256` if the exact images are not installed. Copy the first four
files from this directory and the setup/CLI files from the repository root.
`images.sha256` contains the archive's plain SHA-256 hex digest. Keep runtime
state separate from the source checkout's `app/` and `native/`.

Compose reads `.env` without executing it as shell code. The legacy single-model
profile needs `MODEL_GGUF_PATH`. Optional `GATEWAY_PORT` and `ORDER_PORT` default
to loopback ports 18085 and 18086. Configure public ingress separately.

## Prepare and start

The model-profile input `offer.json` contains:

- `offer_id`, `allowance_credits`, `window_seconds` and immutable `input_rate`,
  `output_rate`, `cache_rate`.
- `default_model`: exactly `registry: "huggingface"`, `repository`, immutable
  `revision`, and one GGUF `file`.
- `model_coverage`: `max_total_parameters`, `max_weight_bytes`, `architectures`,
  `file_types`, `min_context_tokens`, `text_only: true`.
- `strict_text_policy`: rates plus `context_tokens`, `max_output_tokens` and
  `reasoning_effort`. Native checkout also needs `native_runner_id`,
  `native_recipient_address` and `price_wei` on the offer.

The private native configuration contains `recipient` (registered provider
address), `network`, `rpc` and `service_url`. Keep it mode 0600. The configured
chain profile is `arbitrum-one-mainnet`; use an authorized provider identity and
RPC route. Setup can create a dedicated local node account once, or
`--node-account ADDRESS` selects an existing account from the retained keystore.
The provider's primary wallet key is not needed.

From the runtime directory:

```sh
sh punch-stack up --offer ./offer.json --native-config ./private-node.json
sh punch-stack status
sh punch-stack diagnose
```

`up` checks image identities, loads a checksummed archive if necessary, prepares
the model, waits for model/ledger readiness, initializes private credentials,
starts the native node and gateway, and publishes the offer. `preflight` checks
installed images and resolved configuration without loading or starting
services. The legacy no-offer path uses an already verified local GGUF.

## Retained state and shutdown

`newapi-data/` contains the inference-credit ledger. `app/data/` contains order
mappings, initialization and maintenance state. `app/` also holds provider
credentials. `native/` contains the dedicated node keystore, configuration and
chain/ticket state. `models/` holds verified cache, presets and retained default.
Keep these paths together and private.

```sh
sh punch-stack stop
sh punch-stack up
sh punch-stack logs order-api
```

Stop preserves state. Restart reuses credentials and retained offers; it does
not unwithdraw offers or replace conflicting terms. Errors preserve state and
already-started dependencies. Repair the reported boundary and resume the same
operation. Missing custody or corrupt state is a refusal, not permission to
recreate identities. Never delete the ledger, rewind chain history or use
`down -v` to clear uncertainty. A lost inference response may retain a
reservation; restart does not prove that no work occurred.

Automatic updater and release-feed machinery are outside this distribution.
No update feed is configured. Source tests and historical native evidence do
not establish new GPU, funded-chain or public runtime-installation acceptance.
