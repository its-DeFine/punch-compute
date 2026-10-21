# Provider and client contract

Punch's HTTP APIs are the integration contract. The optional CLI maps those APIs to convenient commands and keeps credentials and purchase recovery material in private local files. It must not introduce a second ledger, separate authorization rules or a CLI-only purchase path. A gateway, application or agent can implement the same documented protocol.

## Roles and access

The seller publishes and withdraws offers, operates the Compose stack. The buyer owns the purchased order and can request model changes within its saved coverage. A buyer's chosen Livepeer payer/signing service pays for the order; our AWS wallet is not required. A gateway may act for a buyer only with that buyer's authorization.

Buyer account authentication manages the order and retrieves its inference key. The order key authorizes inference; it does not itself grant model-management or seller privileges. Wallet addresses and caller-supplied payment headers are not proof of ownership. Native verified payment must match the prepared purchase before access is granted.

New API is the only inference-credit ledger. Input, output and cached-token charges use the order's saved rates. Model changes preserve the order, key, balance and agreed coverage. Withdrawal prevents new purchases while existing orders retain their agreed rights.

## Seller

Use a verified release bundle. The source checkout does not contain the image archive. See [setup prerequisites and recovery](packaging/BOOTSTRAP-REQUIREMENTS.md) for the current GPU, account, networking and persistence requirements.

```sh
sh punch-stack up --offer ./offer.json --native-config ./private-node.json
sh punch-stack status
sh punch-stack diagnose
sh punch-stack logs order-api
```

`offer.json` declares the default registry model, replacement-model coverage, immutable rates, allowance, validity and native commercial terms. `private-node.json` selects the registered recipient, network, chain RPC and node service origin; keep it private. Setup selects or creates and saves the dedicated native node/redemption account. Do not substitute the provider's primary wallet key.

To publish a new explicit offer revision or withdraw an offer, set `PUNCH_ORDER_GATEWAY` to the seller's order API origin and use:

```sh
bin/punch-token-orders offer-publish --seller-token-file ./app/seller.token --offer-file ./prepared-offer.json
bin/punch-token-orders offer-list
bin/punch-token-orders offer-withdraw --seller-token-file ./app/seller.token --offer-id OFFER_ID
```

The setup `--offer` input includes registry model preparation. The direct `offer-publish --offer-file` command sends the already-prepared offer API body; it does not perform that preparation itself. Keep these file formats distinct until their exact schemas/examples are included in the accepted release guide.

| Operation | Order API | Authorization |
|---|---|---|
| List offers | `GET /offers` | Public |
| Inspect offer | `GET /offers/{offer_id}` | Public |
| Publish | `POST /offers` | `seller_token` in JSON body |
| Withdraw | `POST /offers/withdraw` | `seller_token` and `offer_id` in JSON body |

## Buyer

A provider must supply its order API origin, buyer-authentication API origin, native Livepeer origin/runner and inference origin. These are distinct roles even when one HTTPS hostname routes them. Internal Compose names and administrative endpoints are not public buyer instructions.

The current CLI expects a private mode0600 JSON credentials file with `username` and `password`, and `--api` names the buyer-authentication API origin. It logs in through New API and saves its session locally. Native self-registration has passed an isolated real-API check with two independent buyers. Submit `POST /api/user/register` with JSON `username` and `password` to the provider-authentication API, then log in with those same fields. Require a successful response before proceeding; registration may be disabled or require additional verification under provider settings. Public ingress is deployment configuration; no convenience signup command is supplied.

### Inspect and prepare

With `PUNCH_ORDER_GATEWAY` pointing to the provider order API:

```sh
bin/punch-token-orders offer-list
bin/punch-token-orders purchase-prepare --api BUYER_AUTH_ORIGIN --buyer-creds ./buyer.json --offer-id OFFER_ID --idempotency-key PURCHASE_ID --payer-address PAYER_ADDRESS --output ./purchase.json
```

Preparation creates and saves an immutable order and purchase intent. It does not pay, grant inference or verify control of the supplied payer address. Retain the same purchase ID and private file for recovery.

### Pay and obtain access

The supported native protocol components require the client to:

1. Read the prepared order and verify its provider, payer, amount, terms and expiry.
2. Request the provider's fixed purchase challenge with that order's purchase intent.
3. Ask the buyer-selected authorized Livepeer signer to generate the payment, verifying it matches the accepted challenge and amount.
4. Persist the exact payment and segment material before delivering it to the native paid activation route.
5. Recover the same order through authenticated readback; repeat delivery only using the same persisted payment when the protocol permits it.

The integrating buyer client owns challenge, signing and durable delivery. The workload CLI supplies preparation and readback only; purchase-challenge and purchase-deliver are not included. Do not substitute `order-acquire`: that is the unfunded fixture path and is disabled when live checkout is enabled. A lost signing response must not trigger a fresh ticket automatically.

After successful native activation, authenticated purchase readback returns the order key and inference information. The current CLI saves the key without printing it when `--output` refers to the matching preparation file:

```sh
bin/punch-token-orders purchase-status --api BUYER_AUTH_ORIGIN --buyer-creds ./buyer.json --order-id ORDER_ID --output ./purchase.json
bin/punch-token-orders order-status --api BUYER_AUTH_ORIGIN --buyer-creds ./buyer.json --order-id ORDER_ID
```

| Operation | API boundary | Authentication / input |
|---|---|---|
| Self-register | New API `POST /api/user/register` | Buyer-chosen username/password; regular account, no order credit grant |
| Login | New API `POST /api/user/login` | Buyer username/password; returns a buyer session |
| Prepare | Order API `POST /purchases/prepare` | JSON `buyer_token`, `offer_id`, `idempotency_key`, `payer_address` |
| Paid activation | Native `/apps/{runner_id}/app/purchases/activate` | Native fixed purchase/intent/payment/segment protocol plus buyer session and order ID; direct unsigned wrapper calls cannot replace it |
| Purchase readback | Order API `GET /purchases/{order_id}` | Buyer session bearer token; successful ready response can contain the sensitive order key |
| Balance/usage | Order API `GET /orders/{order_id}` | Buyer session bearer token |

A compatible buyer client sends `POST /apps/{native_runner_id}/app/purchases/activate` with `Livepeer-Payer-Address`, `Livepeer-Fixed-Quote: purchase`, and `Livepeer-Purchase-Intent`. The native402 JSON contains `payment_params` (base64 OrchestratorInfo protobuf), `manifest_id`, `orchestrator`, `payment_url`, and `commercialPrice`. It is saved privately, not followed as a routing instruction. A repeated challenge is not a replacement for recovering an uncertain signing operation.

The chosen native remote signer's `POST /generate-live-payment` accepts `orchestrator` (the challenge's `payment_params`), `ManifestID` (its `manifest_id`), `app: "punch/compute-v1"`, `type: "fixed"`, and `maxPrice: {"price": PRICE_WEI, "currency": "wei", "unit": "fixed"}`. It returns `payment`, `segCreds`, and signed `state`. Use an authorized signer for the prepared payer and independently verify the protobuf quote's recipient, amount, maximum winning probability and one-ticket constraint against the accepted order before authorizing signing. Delivery does not replace those signer checks or parse/verify ticket cryptography. Existing generic clients must support this fixed-purchase extension; compatibility with arbitrary upstream gateways is not implied.

Save that original signer response as a private0600 JSON file. The buyer client must persist its exact payment/segment bytes and pinned order/native origin before sending, then use the same app POST with `Livepeer-Payment`, `Livepeer-Segment`, quote/intent headers, buyer bearer authentication, and JSON `order_id`/`buyer_token`. A successful native activation returns `success: true` and `data` including `state: "READY"`, the same order ID and original `order_key`. The native orchestrator and wrapper verify payment and ownership before granting allowance.

On timeout/failure, retain the prepared file, signer response and delivery journal. After reconciliation, recover using those same materials; never sign another ticket merely because a response was lost. The original native origin and order/payment fields must match; mismatches fail before network dispatch. Unknown outcomes stay unknown. The order key can also be recovered through authenticated `purchase-status`. Use verified HTTPS origins for public integration.

The external client must implement this protocol; arbitrary upstream client compatibility is not implied.

### Inference and model selection

Use the returned inference endpoint and order key for text chat completions. The request's model must match the saved order model. Messages contain only role (system, user or assistant) and string content. The strict profile accepts model, messages, max_tokens, max_completion_tokens, n, stream, stream_options, temperature, top_p, stop, seed and reasoning_effort. It requires n=1; output limits must be positive integers no greater than the saved policy maximum, and both limit fields must agree if supplied together. Reasoning effort must match the saved profile. Tools, image/audio content, other roles and unknown fields are rejected. Keep the order key in client configuration, outside logs and command-line arguments.

Model changes use buyer account authentication and an immutable registry revision:

```sh
bin/punch-token-orders order-model-select --api BUYER_AUTH_ORIGIN --buyer-creds ./buyer.json --order-id ORDER_ID --repository HF_REPOSITORY --revision IMMUTABLE_REVISION --file MODEL_GGUF --idempotency-key MODEL_CHANGE_ID
bin/punch-token-orders order-model-status --api BUYER_AUTH_ORIGIN --buyer-creds ./buyer.json --order-id ORDER_ID
```

These map to `POST` and `GET /orders/{order_id}/model` with a buyer bearer token. The POST body is `registry: "huggingface"`, `repository`, `revision`, `file` and `idempotency_key`. Verified coverage determines eligibility; missing specifications do not count as compliant. Read model-operation status before assuming a requested model is serving. Retain the operation identity across retries. A failed load should restore the prior model and report restoration failure explicitly; it must not silently serve another model as the requested one.
