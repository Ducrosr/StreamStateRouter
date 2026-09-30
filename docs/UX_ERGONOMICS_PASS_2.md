# UX ergonomics pass 2

## Purpose

This branch reorganizes the SSR desktop interface so the user can answer four
questions without understanding the internal routing architecture:

1. What is SSR doing now?
2. Why did SSR choose that state?
3. Is anything wrong or pending?
4. What should I do next?

The change is intentionally limited to UI presentation, navigation and guided
workflows. It does not redesign routing, OBS serialization, recovery, A1 helper
ownership, A2 identity semantics or the declarative execution model.

## Information architecture

### Simple mode

The primary navigation is reduced to:

- **Accueil** — operational state and the next useful action;
- **Automatisations** — human-readable "when → then" behavior;
- **Configurer** — guided configuration by user intent;
- **Paramètres** — common application/OBS/Windows settings.

Rules, Profiles, Layouts, Diagnostics and the technical Journal remain hidden
until Expert mode is enabled.

### Expert mode

Expert mode adds:

- **Règles**
- **Profils**
- **Layouts**
- **Diagnostics**
- **Journal**

Expert mode does not change runtime behavior. It only exposes the technical
objects and maintenance surfaces.

## Persistent operational state

The window header separates four independent concepts that were previously
mixed together as buttons:

| Area | State shown | User action |
| --- | --- | --- |
| Interface | Simple / Expert | switch presentation depth |
| Configuration | Protected / Editing | enter or leave draft editing |
| Routing | Automatic active / Suspended | suspend or resume routing |
| OBS | Connected / Disconnected / Disabled / Incomplete | status only |

Buttons describe the action they will perform; adjacent labels describe the
current state.

When edit mode is active, a persistent banner states that automatic routing is
frozen and changes are being made to the draft.

## Draft, saved configuration and runtime

A second persistent banner makes configuration state explicit.

When the draft is dirty it shows:

- the number of detected changes;
- their high-level categories (rules, profiles, layouts, etc.);
- **Review**, **Discard** and **Save and apply** actions.

When the saved and applied revisions differ, the banner explains that the
runtime still needs to be synchronized.

The existing detailed draft review remains the final consequence preview before
saving/applying.

## Accueil as the cockpit

Accueil is no longer the home for every maintenance tool. Its permanent content
is limited to:

- global health;
- current decision and reason;
- clickable decision trail;
- desired vs applied domain state;
- a single context-sensitive recommended action;
- detected OBS drift when relevant;
- recent user-facing activity;
- foreground application.

The decision trail follows:

    foreground app → selected rule/fallback/override → effective profiles

Rule/profile/layout steps navigate directly to the corresponding Expert object.

The recommended action is prioritized from the current state, including:

- configure OBS;
- test a disconnected OBS connection;
- review a dirty draft;
- synchronize a saved/applied revision mismatch;
- resume suspended routing;
- clear a manual override;
- correct OBS drift;
- diagnose blocked/failed state;
- apply pending differences.

Where possible, corrective actions display the number of differences that will
be re-evaluated before mutation.

## Guided configuration

The **Configurer** surface is organized by intent rather than by internal SSR
object type.

It provides:

- **Configure current application** — automatically enters Edit mode before
  launching the existing safe current-state capture workflow;
- **Analyze OBS collection** — read-only analysis;
- **Advanced import tools** — escape hatch to Expert workflows;
- **Examine OBS references** — guided repair path;
- **Check capabilities** — read-only readiness diagnostics.

A user can therefore configure a normal application without first learning the
Rule/Profile/Layout data model.

## Action-risk language

The UI uses one consistent visual vocabulary:

- **blue** — read-only inspection;
- **gold** — modifies the SSR draft only;
- **red** — may mutate OBS immediately.

This is supplementary information. Safe Live confirmations and existing runtime
guards remain authoritative.

## Expert screen simplification

### Rules

Frequent actions remain visible: Add, Modify and Test. Duplicate, enable/disable
and delete move under **⋯**.

### Profiles

Frequent actions remain visible: New, Modify action and a contextual
**Test "<profile>" in OBS** action. Profile management and infrequent action
operations move under **⋯**.

The screen explicitly states that ordinary edits affect only the draft while
direct testing is an immediate OBS operation.

### Layouts

Layouts are split into three conceptual stages:

1. **OBS source — read only**
2. **SSR layout — draft**
3. **Layout content — modules and geometry**

Actions use consequence-oriented wording:

- **Read / synchronize OBS**
- **Capture OBS → "<LayoutProfile>"**
- **Apply "<LayoutProfile>" to OBS**

Less common preview, rollback, comparison, validation and management operations
are grouped under **⋯**.

## Diagnostics and Journal

Advanced maintenance moves to **Diagnostics**, which contains:

- guided diagnosis;
- health/capability report;
- provenance ("Who controls what?");
- dependency tree;
- OBS reference repair;
- manual operation rollback;
- user-facing activity.

The Journal remains available as technical evidence, not as the normal answer
to a user-facing failure.

## Search / command palette

The existing Ctrl+K palette remains the fast Expert navigation path and now also
includes Accueil, Configurer and Diagnostics. Rules, profiles and layouts remain
directly searchable.

## Safety invariants

This UX branch must not:

- change routing decisions or priorities;
- issue OBS I/O from new Qt code paths except by invoking existing guarded
  actions;
- bypass Edit mode for draft mutations;
- bypass Safe Live for immediate OBS mutations;
- modify A1 recovery/helper semantics;
- implement A2 durable identity;
- change runtime worker serialization.

Guided wrappers may enter Edit mode on behalf of the user, but they still call
the existing guarded workflows afterward.

## Validation gates

Automated:

- full Python unit suite;
- Ruff;
- configuration/CLI smoke tests;
- Stream Deck typecheck/build/package;
- CodeQL;
- pure presentation tests for header state, draft state, decision trail and
  contextual action priority.

Real Windows/OBS UX pass before merge:

- Simple mode exposes only Accueil, Automatisations, Configurer and Paramètres;
- Expert mode exposes all technical tabs;
- Edit mode visibly owns/releases routing pause correctly;
- dirty draft and saved/applied mismatch banners are accurate;
- decision trail links open the intended rule/profile/layout;
- Configurer current application enters Edit mode exactly once and launches the
  existing capture workflow;
- read/draft/live action styling matches actual side effects;
- Layout wording never confuses reading OBS, capturing to draft and applying to
  OBS;
- contextual recommended action matches the current operational problem;
- Ctrl+K still navigates to all objects;
- Journal remains accessible but is not required for normal diagnosis.

## Astra review focus

Astra should review this branch as a UX/information-architecture change, with
special attention to:

- whether any new UI callback accidentally weakens an existing safety gate;
- whether Simple mode hides information required for informed action;
- whether recommended-action priority can lead to an unsafe or misleading next
  step;
- whether the action-risk vocabulary matches actual side effects;
- whether guided auto-entry into Edit mode preserves pause ownership;
- whether any Expert function was lost rather than merely moved.
