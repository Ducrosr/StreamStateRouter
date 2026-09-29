# E1 preparation — explainability and bounded offline replay

Preparation only. Do not implement before the B0 contracts are stable.

## Existing base to preserve

SSR already records useful structured runtime information:

- `RuntimeEvent`
- routing-decision status/history
- activation diagnostics
- request status
- desired/planned declarative diagnostics
- runtime/user activity presentation
- simulation capabilities in parts of the current stack

E1 should make these records reproducible and explainable, not replace them with a full event-sourcing architecture.

## Product questions E1 must answer

- Why is this setup active?
- What triggered the last change?
- Which rule/profile/fragment won?
- What did SSR intend to change?
- What did it actually write?
- What was verified?
- What is pending, blocked or uncertain?
- What can the user do next?

Simple explanations should use user concepts, not internal class names.

## Bounded diagnostic record

Capture only the data needed to reproduce resolution/planning.

Candidate record:

- format/schema version
- timestamp/relative sequence
- foreground/app context
- relevant control variables
- stream/recording/OBS context used by conditions
- config revision or redacted configuration fingerprint
- effective input intent
- rule checks/resolution result
- desired-state/provenance summary
- planner result/diagnostics
- execution result if the record includes a live run
- external observation freshness/session generation needed for interpretation

Do not store secrets or arbitrary raw input/filter settings by default.

## Offline replay boundary

Replay should execute:

- pure rule/profile resolution
- desired-state construction
- pure planner logic when supplied with captured observations/capabilities

Replay must NOT:

- reconnect to OBS
- write OBS
- write Windows state
- run SoundVolumeView
- trigger media
- send API/Stream Deck commands

If captured evidence is insufficient, replay result must be "incomplete/unknown", not a fabricated verdict.

## Determinism

Given:

- same replay format version
- same logical inputs
- same relevant config snapshot/revision
- same captured observations/capabilities

the replayed resolution/plan should be deterministic.

Time-based behavior requires explicit simulated time, not wall-clock reads.

## Retention

Avoid an unbounded event log.

Candidate strategies for later gate:

- bounded in-memory recent history
- explicit diagnostic export
- optionally bounded persisted recent session when justified

Do not choose long-term retention/privacy policy silently.

## Redaction

Never export by default:

- OBS password
- API token
- arbitrary input-setting contents that may contain URLs/cookies/tokens
- other known secret-bearing settings

Reuse existing diagnostic redaction boundaries.

## "Why?" view

Simple output example:

"Overwatch est actif. La configuration Overwatch est utilisée. L'humeur Focused ajoute l'avatar Focused. Le layout est appliqué. Le réglage audio attend une session du jeu."

Advanced expansion:

- stimulus
- matched rules
- provenance
- desired properties
- observed values
- planner decisions
- writes/readbacks
- warnings/recovery

## Tests

- same captured record -> same resolution
- same record -> same plan
- event ordering
- simulated time
- missing observation
- missing capability
- old replay format
- unknown/newer format
- truncated record
- redaction
- config revision mismatch
- replay has zero external I/O
- conflict explanation preserves provenance

## UX budget

Daily input: zero.

Actions:

- "Pourquoi ?"
- "Exporter le diagnostic"

No setup wizard fields added.

## Gate

Astra reviews:

- minimum evidence needed for faithful replay
- retention
- privacy/redaction
- strict no-I/O replay boundary
