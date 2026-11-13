# Record editor protocol operation pick list

Base: tnunamak/azizam cfe07ef445e8459bda53d0c135ef33228414842b.

## Deliverable
Exactly **3 Vitest cases added**, all `it.fails`, in engine-record-page.spec.tsx:
1. Description operations chat/smell.infer are shown, with only chat ticked.
2. Tick smell.infer and save capabilities exactly [chat, smell.infer].
3. Untick chat with smell.infer still unticked and save capabilities exactly [].

Expect it to work because: existing native protocol specs already mock the catalog and mount this record page; the add-dialog spec uses named operation checkboxes and capabilities payloads. Confidence: 80% in the static product-gap finding, 60% in unexecuted test integration.

## Verified product gap and saving
The editor DOES render a capability pick list, but NOT one built from description operations. engine-record-page.tsx:281–288 loads/selects the description for validation; :319 seeds ticks from view.capabilities. Native rows at :584–589 map definition.capabilities, and :1004–1020 render CapabilityList with those rows. saveBody at :376–388 filters selected IDs through definition.capabilities (:384), dropping novel operations. save at :550–563 routes offering changes to enginesApi.update(service_id, body); name-only edits use api.services.edit. Save changes at :1180–1187 is appropriate for a capabilities-only edit with unchanged native connection.

The permitted add-engine/add-engine-protocol.spec.tsx:127–163 documents the reusable dialog contract: :134 declares operations, :155–158 selects operation checkboxes, :159–162 expects selected capabilities. The dialog implementation that builds the list is NOT in this assignment's read allow-list, so its implementation file:line could not be verified. No outside-scope implementation citation is invented. Owner can supply/authorize that source for an exact extraction location.

## Tests and uncertainty
Tests were NOT run, as instructed. No build, runtime validation, or product fix is claimed. No product code changed. Both expected-failure UI and save paths are executable test code, not pseudocode.
Least-certain assertions:
- Protocol description combobox accessible name and initial selected value local-chat: existing specs establish the name, but this saved-value setup has not been executed.
- Exact accessible operation names: smell.infer follows the dialog contract; chat uses case-insensitive exact matching to allow existing Chat label.
- Save changes becoming available/enabled after only an operation edit depends on the future product fix making saveBody include described operations; current source necessarily fails before that point.
- Exact payload order [chat, smell.infer] assumes description order; [] explicitly requires clearing rather than omitting capabilities.
- All three .fails cases presently fail at the missing smell.infer control by static inspection, not an observed Vitest result. Setup is outside .fails so setup failures cannot masquerade as the product gap. Vitest expected failures still accept unrelated failures inside their bodies; remove .fails after implementing the list/save fix and inspect failure reasons before accepting the suite.

## Owner validation
Run the record spec with the repository's existing Vitest command. Check that these three expected failures are specifically missing described-operation controls. Implement the separate product fix, remove .fails, and verify payload assertions pass. Keep other protocol-honesty cases green. No publication/deployment requested or performed.

## Resolution (owner, 2026-11-13)
Confirmed the gap with Vitest: all three cases failed at the missing smell.infer checkbox. It was also a data-loss bug: a save from the record editor dropped every described task that the engine definition does not name (saveBody filtered through definition.capabilities). Fixed in engine-record-page.tsx: with protocol dispatch, `capabilityChoices` is the chosen description's operations, then any saved task the description does not declare (so loading the page is not an edit and that task can still be unticked). Rows, the save body, the model kinds and the access preview all read `capabilityChoices`. Until the description loads, the save body sends the saved tasks unchanged; protocol-mode capabilities are sorted, so description order does not make the page dirty. The three cases are now plain `it` cases and pass; a fourth case covers the undeclared saved task and fails without the fix (mutation-checked).
