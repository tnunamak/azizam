# Live paid-provider cost and authentication proof

Date: 2026-10-03 (America/Chicago); requests crossed into 2026-10-04 UTC.

Used by: the paid-proof lane report and the Providers positive-spend display fix.
This note records measured behavior, not a general provider reliability claim.

## Question and local evidence

Can an operator paste encrypted provider credentials, restrict paid requests to
one access plan, enforce provider/global caps, and inspect real request costs?

Local evidence consulted: the external research index's Infisical CLI cache
entry and TypeSafe integration entries; this worktree's TypeSafe facade-fidelity
and provider-catalog/key-custody entries; provider configuration, admission,
execution, receipt and activity API code; the product strategy and lane brief.
The local provider catalog correctly leaves missing listing prices unknown.
Current prices still needed verification before actual spending.

## Primary sources read

| Source | Relevant facts | Use and limitations |
| --- | --- | --- |
| [TypeSafe documentation index](https://docs.typesafe.ai/llms.txt) | Identifies Models, API and Noul documentation. | Discovery only; no price assertion from the index. |
| [TypeSafe Models](https://docs.typesafe.ai/models.md) | At fetch time, `jev-latest` resolved to `jev-1.13.0`; input cost was $0.042 per million tokens, output cost zero. Model listing returns aliases and metadata without prices. | Author the verified rate as operator data; this is a public rate, not an independently inspected account invoice. |
| [TypeSafe API](https://docs.typesafe.ai/api.md) | System One uses `state`, `model` and named typed questions; responses expose resolved model, answers and token usage. | Build two independent Noul questions and calculate charge from actual input usage. |
| [TypeSafe Noul](https://docs.typesafe.ai/primitives/noul.md) | Noul evaluates a yes/no proposition and returns its probability. | Separate account-access and billing propositions. No claim of classifier validation follows from this smoke test. |
| Live product TypeSafe checks | Authenticated listing succeeded for both pasted and stored keys; listed `jev-latest` and `jev-preview`, with unknown prices. | Proves credential/model discovery through the product; the check does not perform inference. |
| Live product OpenRouter checks | The primary key failed its auth probe. An alternate Infisical OpenRouter key passed and listed 466 models. `openai/gpt-4.1-nano` listed input/output rates of $0.10/$0.40 per million tokens. | Choose one low-cost non-batch model using the actual model catalog; do not mistake public model listing for key validity. |

## Measurements

| Case | HTTP result | Request ID | Wall latency | Accounted cost |
| --- | --- | --- | --- | --- |
| First authenticated TypeSafe request | 200, two answers, 302 input tokens | `req_ed33e51676a243fd9d34ff086fe1fb29` | 542.30 ms | Initially unknown; ledger manually settled at $0.000012684 using published rate |
| Final TypeSafe request after explicit versioned-price data | 200, two answers, 302 input tokens | `req_d5b16fcc6d82454d9ca039d51ab790d1` | 547.44 ms | $0.000012684, `price-table`, settled |
| Plain local `jev-latest` | 200, `decider-2b-v11`, CPU, remote false | `req_952b512fff1a4b758c7f37002aaff075` | 1122.05 ms | $0, metadata `free-local` |
| Unpermitted plan | 403, protocol not permitted | `req_fa69b9a8b482413b9907151888b5fb12` | 92.73 ms | No spend change; no paid dispatch |
| Invalid question type | 422, path-specific validation detail | `req_41efc27e23f04d74aea226913c1f07ff` | 6.35 ms | No spend change; remote false |
| OpenRouter | 200, `paid proof ok` | `req_fffa5e1ed0bb48b48d054d8b32443454` | 2464.28 ms | $0.000003000, `provider-reported`, settled |

All final receipt reads returned 200 using the owning App credential. Final
TypeSafe spend was $0.000025368, OpenRouter $0.000003000, global $0.000028368.
No unresolved reservations remained. TypeSafe caps were $1/day and $1/month;
OpenRouter $0.20/day and $0.20/month; global $2/day and $2/month. Both providers
allowed only the existing owner plan. Temporary proof App keys were revoked.

## Negative results, traps and reasoning

1. `web.run` fetched the documentation index but returned inaccessible-page
   errors for the three Markdown pages. Python urllib fetched all three.
   A fetch failure did not mean a page was absent.
2. The initial browser had no session. The subsequently approved admin key had
   `admin`, not `owner`; owner-only all-request Activity remained unavailable.
   App-owned runtime receipts provided the required evidence without role changes.
3. Localhost LAN bypass was evaluated before explicit App credentials. An
   invalid key could read local model discovery without forwarded origin data.
   Under the configured trusted-proxy path, a non-LAN test client address and
   that same invalid key returned 401. Real proof calls used valid App keys and
   this forwarded test address; no bypass or live authentication setting changed.
4. TypeSafe's versioned response did not match the generated public alias price
   key. The first call succeeded with unknown settlement. Adding a price-table
   entry for the exact returned version through the registry product API made
   the next receipt settle automatically. Vendor/version facts remain data.
   A future alias version requires its own verified rate entry; this proof does
   not establish perpetual cost reconciliation.
5. Manual reconciliation settled the first ledger row, while its saved receipt
   retained its original unknown-cost snapshot. Do not present that receipt as
   automatically settled; use the second call as the settled-receipt proof.
6. The existing OpenRouter row was disabled, unpriced and had no spend or models.
   The key API could not change its env reference to encrypted custody. The
   product delete/create flow replaced that unused row. No unrelated provider
   was changed.
7. Providers rendered every positive charge below half a cent as `$0.00`.
   Six decimal places also hide sufficiently small positive charges. The lane
   fix uses an explicit sub-cent label and preserves normal zero/cent display.
   The live UI is evidence of the defect; deployment of the branch is separate.

## Evidence and confidence

Secret-free operation JSON, screenshots, exact test results and the complete
step matrix live in the orchestrator's `PAID_PROOF.md` evidence directory.
Confidence is high in observed HTTP statuses, routing, persisted receipts and
ledger state. TypeSafe cost is a usage/rate calculation; only OpenRouter
reported the monetary charge directly. Provider invoices were not inspected.
