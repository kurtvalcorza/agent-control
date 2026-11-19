# Capability and planner providers

## Capabilities

A provider implements the `Capability` contract by exposing a `CapabilityDescriptor`,
`invoke(arguments)` and `health()`.

The descriptor is part of the control and safety contract:

- capability name/version/provider ID;
- input/output schemas;
- side-effect class and reversibility;
- required permissions and risk class;
- estimated cost/time/model-call/tool-call usage;
- idempotency metadata.

The host supplies a permission set and maximum capability risk to `CapabilityRegistry`. Provider
resolution then filters in this order:

1. abstract capability name;
2. exact side-effect-class compatibility with the plan;
3. permission/risk authorization envelope;
4. remaining hard run budget;
5. deterministic cheapest/lowest-latency provider ordering.

A planner cannot broaden the authorization envelope by naming a capability or provider.

Available transport boundaries:

- `InProcessCapability` for deterministic tests/local embedding;
- `SubprocessCapability` for JSON-over-stdin/stdout execution with `shell=False`;
- `MCPCapability` for an SDK-independent MCP tool transport callback;
- `AgentRouterCapability` as the reference boundary for the sibling router CLI.

`CapabilityRegistry.health()` exposes provider health/configuration metadata for operators and
`run inspect` includes that information. Transport exceptions are converted into control-plane
failures/observations rather than being treated as successful tool results.

`CapabilityResult` always supports actual dollar cost and may additionally report actual elapsed
milliseconds, model calls and tool calls. Providers that cannot measure those fields leave them
unset; the runtime retains descriptor estimates as the conservative envelope.

Provider-native provenance remains authoritative. Return durable IDs/digests in
`CapabilityResult.metadata` and receipt artifact/precondition references when the control-plane
audit trail should cross-reference provider artifacts or evidence.

## Planner

A planner implements:

```python
create_plan(*, run_id, goal, version) -> Plan
revise_plan(*, run, reason, version) -> Plan
```

Planner output is untrusted. Before acceptance the runtime validates:

- DAG structure/dependencies and canonical unique node identifiers;
- complete success-criterion and requested-output coverage;
- next plan version and correct run ID;
- mandatory capability availability;
- provider side-effect compatibility;
- provider permission/risk eligibility;
- secret-reference rules.

Planning itself is part of the run resource envelope. A planner may publish these attributes:

```python
estimated_cost_usd_per_plan = 0.0
estimated_elapsed_ms_per_plan = 0
model_calls_per_plan = 1
```

Absent a declaration, the runtime conservatively counts one model call per plan/replan. A
fully deterministic planner should declare `model_calls_per_plan = 0`; the built-in
`StaticPlanner` does so. Planning is rejected before invocation when its declared resource profile
cannot fit the remaining run budget, and an attempted planner call is charged even if its proposed
plan is rejected.

The planner cannot invoke capabilities, authorize side effects, change budgets, record
verification, mutate state, or bypass a policy decision.

`StaticPlanner` is provided for deterministic tests and hosts that construct plans outside the
runtime. No provider-specific LLM planner is required by the core.

## Verifiers

`VerificationSpec` has one primary `kind` and may include `additional_kinds`. The
`VerifierRegistry` evaluates every configured kind and aggregates fail-closed: a node passes only
when **all** required verifiers pass. A missing verifier is `BLOCKED`, never PASS. Policies can
require one or several verification kinds; the plan must configure the full required set before
execution is admitted.

If execution output is already persisted and verification later becomes available, hosts may call
`reverify_node()` (or CLI `verification retry`) to verify the same output without invoking the
capability again.

### External observers and observation resolvers (RFC #6 §13)

For a delegated commit the `EXTERNAL_STATE` verifier's subject is an `ExternalObservation`
acquired by the dispatching run, never the executor's output (see `docs/recovery.md`). Two
host-supplied components take part, and they are deliberately separate:

- `ExternalStateObserver` (`ControlPlane(external_observer=...)`) receives run/node/intent context
  and returns an `ExternalObservation`. With `state` set, the verifier sees that state directly.
- `ExternalObservationResolver` (`ControlPlane(observation_resolver=...)`) re-fetches a
  **reference-only** observation (`state=None`). `resolve(reference)` receives only
  `ExternalObservation.reference()` (`ref`, `observer`, `observed_at`, `verification_method`): no
  run context and no executor output. It returns an `ObservationResolution` with the re-fetched
  `state` and its own provenance (`resolver`, `resolved_at`, `method`, optional `metadata`). Because it needs only the
  reference, a later `reconcile_action()` can re-fetch the same reference.

A reference-only observation is `BLOCKED` before any verifier sees it when no resolver is
registered, when the registered resolver is the observer object itself, when `resolve()` raises,
and when it returns `None` or a resolution without state. A resolved observation is verified like
a direct one (PASS = the effect is observed, FAIL = it is observed absent), and its evidence
records `observation_resolution` (`resolver`, `resolved_at`, `method`, `metadata`; not the state)
next to `independent_observation`. A direct observation never consults the resolver.

The observer-object check is a tripwire for accidental reuse of the same instance, not proof of
independence: a wrapper around a shared backend passes it. Independence rests on the resolver
receiving only `reference()`, never run context or executor output.

## Capability sources and pre-resolution (RFC #6 Phase A)

A capability may additionally have a **source**: the portable skill or protocol the provider
executes. The control plane does not define how such sources are packaged. A host-supplied source
adapter implements `CapabilitySource` and is registered per capability name in a
`CapabilitySourceRegistry` passed to `ControlPlane(capability_sources=...)`:

- `load(capability) -> CapabilityContract` returns normalized evidence for the current source
  snapshot: its `ResolvedCapabilityProvenance`, whether semantic closure is complete (and what is
  missing), required vs present governance, mandatory `RuntimeRequirement`s, stable
  `CapabilityClause`s (optional or mandatory, each with its own runtime requirements) and whether
  execution may be delegated;
- `check(recorded) -> SourceCheckResult` re-derives the current identity of a recorded source
  from the source itself (`MATCH` / `MISMATCH` / `UNAVAILABLE`). The runtime compares the returned
  identity itself; a `MATCH` without a freshly derived identity is treated as `UNAVAILABLE`.

**Adapter obligation (not mechanically enforceable).** `check()` must compute `current` from the
source as it exists now, for example by re-reading the payload and calling `canonical_digest`.
The runtime catches the mechanical failures: no `current`, a `current` that differs from the
recorded identity, and handing back the very object it was given. It cannot tell a checker that
copies the recorded values into a new object from one that re-derives them. A checker that does
that silently defeats resume revalidation, so adapter authors own this guarantee.

Digests are `canonical_digest(payload_bytes)`: lowercase hexadecimal SHA-256 over the canonical
payload bytes the adapter selects (`sha256:<hex>` is available as `provenance.identity`). Untyped,
upper-case or non-SHA-256 digests are rejected.

The runtime-requirement vocabulary is deliberately small: `FILESYSTEM`, `NETWORK`,
`LOCAL_PROCESS`, `BROWSER_CONTEXT`, `GPU`, `PERSISTENT_STATE`. There is no `HUMAN_AVAILABLE`;
human participation stays with `HumanGate` and `VerificationKind.HUMAN`. Hosts declare what they
provide with `ControlPlane(runtime_environment=...)`; the default is the empty set (fail closed).

Before `CapabilityRegistry.resolve()` the runtime pre-resolves the node's capability and records a
`CapabilityPreResolved` event (provenance + result):

| State | Meaning | Runtime effect |
| --- | --- | --- |
| `SATISFIED` | closure, governance and runtime requirements present locally | continue to normal resolution |
| `DEGRADED` | only declared optional clauses are unavailable; `dropped_clause_ids` lists them | continue; clause rule below applies |
| `DELEGATION_REQUIRED` | a mandatory runtime facility is absent locally and delegation is permitted | continue as a delegated commit (see `docs/recovery.md`) |
| `UNSATISFIED` | closure/governance missing, or a facility is absent and delegation is not permitted | run fails with `CAPABILITY_UNAVAILABLE`; nothing is invoked |

Pre-resolution never decides budget, permissions, risk, policy or provider selection; those remain
authoritative in the registry and policy engine afterwards.

**Clause rule.** `VerificationSpec.requires_clause_ids` names the clauses a node's verification
depends on. Before invocation the runtime enforces
`requires_clause_ids ⊆ satisfied_clause_ids`, where
`satisfied_clause_ids = declared_clause_ids − dropped_clause_ids`. A dropped *or* undeclared
(typo, stale, foreign) clause ID marks the node `BLOCKED`. An unsourced capability satisfies no
clause.

**In-run stability.** Within one plan version a capability's source identity must not change; a
different identity at a later node records `INPUT_CHANGED` and blocks until an explicit replan.

`agent-relay` is a coordination/handoff protocol, not a capability provider; nothing in the
runtime registers or invokes it as one.
