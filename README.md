# Punch Compute

Punch Compute is the provider workload for prepaid model inference. Providers
run a Compose stack containing the Punch order gateway, patched New API credit
ledger, llama.cpp model server, and the Punch go-livepeer native node with Live
Runner support. New API owns the inference-credit ledger; the gateway owns
offers, order authorization, model selection and recovery.

This source distribution contains the provider workload and its thin HTTP CLI.
Buyer applications integrate through the [HTTP contract](USAGE.md).

## Run the workload

Deployment requires the exact application images in
[`packaging/compose.yaml`](packaging/compose.yaml), Linux UID/GID 1000, Docker
Compose, NVIDIA CDI and suitable model weights. This checkout does not include
a runtime image archive or a download location for one. It is not a complete
installation bundle. Do not substitute stock images or historical bundles.

When the exact images are available, follow [setup and recovery](packaging/BOOTSTRAP-REQUIREMENTS.md).
From the prepared runtime directory:

```sh
sh punch-stack up --offer ./offer.json --native-config ./private-node.json
sh punch-stack status
sh punch-stack diagnose
```

Native configuration uses an existing provider identity and authorized chain
access. Startup can cause network activity. Offline tests do not launch a
provider, use a GPU, sign a payment or spend funds.

## Test the source

The order gateway Dockerfile pins its Python base and direct model dependencies.
Use Docker with BuildKit support:

```sh
docker build -t punch-workload-test -f packaging/Dockerfile.order-api app
docker run --rm --network none --read-only --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=256m,uid=1000,gid=1000 \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/src/app:/src/packaging \
  -v "$PWD:/src:ro" -w /src punch-workload-test \
  python3 -m unittest discover -s tests -v
```

These tests exercise provider logic, the actual gateway and CLI on loopback,
and synthetic native/ledger boundaries. The native binary parser fixture
explicitly skips without its designated image. A source test image rebuilt
today does not prove byte identity with the deployment image, GPU inference,
chain acceptance or a clean runtime installation.

## Source and licensing

First-party code is Copyright 2026 Atumera LLC, licensed under
[Apache-2.0](LICENSE). [NOTICE](NOTICE) identifies third-party exceptions.
go-livepeer retains MIT licensing; New API and its patch retain AGPL-3.0.
Their license texts are preserved verbatim in `native/licenses/`.
Exact commits and patch hashes are in [`native/sources.json`](native/sources.json);
[reconstruction instructions](native/README.md) describe corresponding sources.
Models and upstream container dependencies retain their own licenses.
