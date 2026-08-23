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
