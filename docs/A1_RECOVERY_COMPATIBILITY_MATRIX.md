# A1 recovery compatibility and fault matrix

Preparation only. This document does not authorize A1 before A0 approval.

## Persistence model

A1 should keep two independent persisted concepts:

1. persistent helper ownership manifest
2. transient runtime cleanup obligations

The runtime marker must reference ownership evidence; it must not become the ownership database itself.

## Existing compatibility baseline

Current recovery schema is v2.

Existing cleanup families:

- legacy activation hide without an explicit kind
- `activation_hide`
- `layout_fade`

The current normalizer preserves unknown fields because it copies the input mapping before normalizing `kind`. Preserve this useful forward/backward-compatible behavior.

## Recommended v3 compatibility interpretation

Do not infer ownership merely because a record was loaded under the new application version.

### Old activation rows

Keep existing behavior.

A missing `kind` with an activation target remains normalizable to `activation_hide`.

### v2 layout_fade row

Typical evidence:

- `kind=layout_fade`
- `source`
- `collection`
- optional timestamps/attempts/error

Interpretation:

- valid legacy recovery record
- ownership unproven
- not eligible for automatic mutation of a filter
- not eligible for helper creation
- surfaced as legacy/ambiguous recovery work

Do not synthesize a helper_id.

### v3 proven layout_fade row

Minimum relationship:

- cleanup row references `helper_id`
- persistent helper manifest contains exactly that helper_id
- manifest target/context matches the obligation
- currently observed filter matches the manifest's expected exact resource identity constraints

Only then may recovery perform the cleanup operations allowed by the manifest/obligation.

## Proposed terminal states

Recovery needs explicit outcomes even if the public UI later maps them to simpler wording.

### completed_verified

Use when:

- proven helper exists
- neutral state is verified
- disabled state is verified
- required previous fields are restored/verified when the obligation requires them

Action:

- remove obligation durably

### completed_absent

Use only when:

- ownership is proven
- observation confirms the exact expected helper is absent
- no cleanup creation is required

Action:

- remove obligation after the absence observation is considered terminal by the contract

This does not authorize adopting another same-name resource.

### pending_unavailable

Examples:

- OBS disconnected
- active Scene Collection differs
- source cannot currently be resolved
- readback unavailable

Action:

- retain obligation
- update attempt/error diagnostics as appropriate
- no mutation when target context is not proven

### pending_uncertain

Examples:

- write may have reached OBS but readback failed
- response lost
- source/session changed during verification

Action:

- retain obligation
- observe again on next safe opportunity
- do not blindly repeat non-idempotent creation

### ambiguous_legacy

Examples:

- v2 row with no helper_id
- filter named `[SSR] Layout Fade` but no matching manifest
- manifest missing/corrupt for an obligation that claims a helper_id
- cloned/lookalike helper that cannot be uniquely proven

Action:

- no automatic mutation
- no automatic deletion
- no automatic adoption
- expose an actionable diagnostic / later explicit confirmation flow

## Recovery operation capabilities

Create two separate internal capabilities.

### Normal helper lifecycle

May:

- observe
- create a planned helper
- enable
- set opacity
- neutralize
- disable
- verify

### Cleanup/recovery lifecycle

May:

- observe
- neutralize an existing proven helper
- disable an existing proven helper
- restore explicitly persisted fields
- verify

Must not have access to:

- CreateSourceFilter
- ownership adoption
- filter deletion in A1

This separation should be visible in code structure, not just comments.

## Fault injection matrix

| Injection point | Required durable state before fault | Recovery action | Forbidden |
|---|---|---|---|
| Before manifest write | no effect emitted | none | helper creation during recovery |
| After manifest write, before obligation write | no temporary effect emitted | reconcile orphan manifest safely | opacity mutation |
| After obligation write, before Create | obligation exists | observe planned helper identity | blind Create |
| Create accepted, response lost | obligation exists | observe exact planned identity | duplicate Create |
| After Create, before opacity | obligation exists | observe, neutralize/disable if proven | adoption by prefix |
| Opacity write response lost | obligation exists | readback first | assume success/failure |
| During neutralize | obligation exists | retry idempotent neutralization after observation | clear obligation early |
| Neutralized, before disable | obligation exists | disable proven helper | recreation |
| Disable response lost | obligation exists | read enabled state | repeated creation |
| Verified cleanup, before journal removal | obligation still exists | next recovery observes already-safe state then clears | harmful repeated side effects |
| Foreign collection | obligation exists | wait | mutation |
| Source recreated same name | obligation exists | identity/provenance check | transfer ownership by name |
| User filter name collision | no proven ownership | report ambiguity | mutate/delete/adopt |

## Manifest corruption/loss

A1 must define fail-safe behavior:

- invalid JSON -> no ownership proof
- unsupported manifest schema -> no ownership proof
- duplicate helper_id -> no ownership proof for affected entries
- duplicate target entries that violate "at most one owned helper per qualified source" -> ambiguity, no mutation
- manifest entry with empty/non-text identity fields -> invalid
- manifest entry pointing to a missing helper -> observation may resolve safe absence, but must not create during recovery

The recovery journal should remain intact until a terminal state is proven.

## Import/capture compatibility

Snapshot behavior after A1:

- proven helper -> exclude from business filter capture and add a note/count if useful
- unproven `[SSR]`-looking filter -> keep out of automatic "internal helper" classification
- ambiguity -> warning/report, never silent discard

Do not make capture ownership depend on prefix matching.

## Tests to write first when A1 opens

Recovery parser/serialization:

- read v1 legacy activation row
- read v2 layout fade row unchanged
- write/read v3 proven obligation
- preserve unknown additive fields
- reject malformed helper identity without dropping unrelated cleanup rows

Manifest:

- atomic write/read
- malformed JSON
- unsupported schema
- duplicate ids
- duplicate owned target
- exact round trip

Cleanup executor:

- no code path from cleanup to CreateSourceFilter
- absent proven helper
- absent legacy ambiguous helper
- exact name collision without manifest
- manifest mismatch
- foreign collection
- source recreation
- response lost at each mutation/readback boundary
- repeated recovery is idempotent

Import:

- proven helper excluded
- same prefix/name without proof not treated as owned
- mixed user + proven SSR filters

Runtime:

- reconnect retry
- shutdown snapshot
- transfer between runtime replacements
- obligation removal checkpointed only after verified terminal state
