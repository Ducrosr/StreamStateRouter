# G0 preparation — bounded media commands

Preparation only. This is a later lot.

## Current repository boundary

Current supported action types include:

- OBS scene/visibility/filter/input actions
- Windows audio routing
- Windows HDR
- `wait_ms`

There is no general media-control action family in the current supported action set.

This is useful: G0 can introduce a deliberately small media surface rather than inherit a broad macro language.

## Product boundary

SSR may need simple media intentions such as:

- play
- pause
- restart
- stop where OBS supports a clear equivalent

SSR must not become:

- playlist engine
- timeline editor
- keyframe engine
- arbitrary sequence/workflow engine
- animation editor

Complex overlay animation stays in OBS.

## Property vs command

Media actions are generally commands, not persistent declarative properties.

Examples:

- "restart media" is non-idempotent
- "play" may have state semantics depending on OBS/media kind
- repeating after a lost response can be harmful

Do not force them into the same retry model as:

- volume=50%
- filter enabled=true
- HDR=true

## Command contract

For every allowed media command define:

- supported OBS request/kinds
- preconditions
- whether it is idempotent
- response semantics
- observable post-state, if any
- retry policy

Default for non-idempotent commands after uncertain response:

- do not auto-replay
- report outcome uncertain
- allow the user to inspect/reissue explicitly

## Allowlist

Start with a closed set of commands proven useful for the stream.

Do not add a generic "send arbitrary OBS request" action.

Do not add arbitrary waits/sequences around media commands as a user macro language.

Existing `wait_ms` must not become the seed of a general workflow DSL.

## Target binding

Use A2-style resource identity where available.

Do not persist transient IDs as durable media identity.

If target changes/disappears between preparation and execution:

- fail/replan
- do not redirect to a same-name resource blindly

## Runtime

API/UI/Stream Deck submit one media intention.

Runtime serializes execution.

TypeScript never constructs multi-step media sequences.

## Tests

- media source absent
- wrong source kind
- source not ready
- play/pause supported
- ended/error state
- source replaced after preparation
- session/collection changed
- cancellation before emission
- response lost after non-idempotent emission
- repeated user presses are distinct user intentions
- no automatic replay after uncertain result
- shutdown while request pending

## UX target

Button/setup:

- discovered media target
- one action

Daily use:

- one press
- clear pending/completed/uncertain feedback

No timelines, retries, raw OBS request names or scripting.

## Gate

Astra review focuses on:

- command/property boundary
- non-idempotence
- retry behavior
- allowlist size
