# F0 preparation — local API and Stream Deck centered on intentions

Preparation only. Do not implement before the B1/B2 result/catalog contracts are stable.

## Existing base to preserve

The local API already has useful foundations:

- localhost HTTP server
- optional bearer token
- no browser-facing CORS
- body size limit
- `GET /status`
- `GET /requests/<request_id>`
- POST actions routed through one action callback
- HTTP 202 when SSR reports `status=accepted`

The Stream Deck client already:

- submits commands to SSR
- reads `request_id`
- polls request status
- waits while status is `accepted`
- treats `completed` as completion
- treats `failed` as failure
- does not implement OBS business logic
- does not blindly replay long commands after a timeout

Current commands include:

- pause
- auto
- reapply
- set control variable
- apply layout
- preview layout
- cancel preview
- undo layout

This is a strong base. F0 should enrich it, not replace it.

## Product objective

A Stream Deck button represents a user intention.

Examples:

- "Revenir en automatique"
- "Humeur : Focused"
- "Utiliser le setup Overwatch"
- "Aperçu layout X"
- "Annuler l'aperçu"

It should not encode:

- source names
- filter names
- OBS request sequences
- timing/retry chains
- technical profile composition

## Catalog endpoints

Add read-only catalogs from SSR once B1/B2 contracts are stable.

Candidate catalogs:

- setups/app configurations
- LayoutProfiles
- control variables + allowed/current values when known
- manual override release modes
- possibly repairable targets where relevant

Property Inspector should use these catalogs to populate selectors.

Avoid free-text technical names when SSR can enumerate them.

## API result contract

Preserve old clients where possible.

New additive result fields can distinguish:

- accepted
- running/pending
- completed
- partial
- failed
- superseded/cancelled if already represented by runtime semantics
- result uncertain when transport/external verification is incomplete

Do not redefine an existing successful legacy response incompatibly.

Important:

- HTTP response loss after acceptance does not prove command failure
- network timeout does not prove command failure
- partial physical application must not be presented as full success
- request ID is the way to inspect an accepted operation

Persistent idempotency across SSR restart is NOT automatically introduced unless separately designed.

## Request lifetime

Define and document:

- how long completed request status remains queryable
- behavior after SSR restart
- explicit "expired/not found" semantics
- no false reconstruction of a result after state loss

This is a contract decision to review at F0.

## Pause vs Auto

Preserve current semantics:

- Pause suspends automatic routing behavior according to current runtime contract.
- Auto is a manual intention to return to automatic routing.
- Auto can remain meaningful while paused according to the established behavior.

Do not collapse them into one toggle.

## Stream Deck client

Keep TypeScript thin.

Allowed:

- fetch catalogs
- persist selected IDs/aliases needed for the UI
- submit intent
- poll request result
- render status/error

Forbidden:

- choose the winning profile
- build OBS sequences
- implement retries of business operations
- infer fallback
- reproduce SSR rules

## Better long-command handling

Current polling every 100 ms is functional but can later be tuned.

Do not optimize before the server result contract is stable.

Potential additive behavior:

- expose human phase/status
- show "En cours…" instead of blocking-looking feedback
- retain request ID while the button remains alive
- allow refresh/re-read of result

No blind POST replay after ambiguous network failure.

## Catalog UX

Property Inspector target:

Instead of text field:

`Layout: [_______]`

show:

`Layout: [FPS ▼]`

loaded from SSR.

For control variables:

- variable selector
- value selector when SSR knows allowed values
- otherwise constrained free text only if the variable contract genuinely permits it

For setup:

- user-facing setup name
- no GameProfile/OverlayProfile/CaptureProfile tuple.

## API security boundary

Preserve:

- localhost binding by default
- token support
- no browser CORS
- payload limit
- no secrets in catalog/result diagnostics

Do not broaden to a network service as part of F0.

## Tests

API:

- catalog success/failure
- unauthorized
- browser Origin rejected
- oversized body
- accepted request returns request_id
- completed
- failed
- partial
- expired/not found
- lost client response after server acceptance
- long command
- command superseded/stopped if runtime supports it
- Auto during pause

Stream Deck:

- catalog load
- stale selected option
- SSR unavailable
- accepted -> completed
- accepted -> failed
- API timeout after acceptance does not trigger blind retry
- plugin restart while command exists
- no business logic in action layer

## UX budget

Configuration:

- choose one intention target from a list
- connection settings remain advanced/global

Daily use:

- press button
- no confirmation unless the intention itself is destructive/ambiguous

## Gate

Astra review focuses on:

- result semantics
- backward compatibility
- no second business engine in TypeScript/API
- timeout/idempotence semantics
