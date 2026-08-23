# Capability and planner providers

## Capabilities

A provider implements `Capability` by exposing a `CapabilityDescriptor` and `invoke(arguments)`.
The descriptor is part of the safety contract: name/version/provider, schemas, side-effect class,
reversibility, permissions, risk, expected cost/time/call usage, and idempotency metadata.

The runtime resolves the cheapest provider that exactly matches the plan's declared side-effect
class and fits the remaining hard budget. A plan that understates a provider's side effects is
rejected before execution.

Available transport boundaries:

- `InProcessCapability` for tests/local embedding;
- `SubprocessCapability` for JSON-over-stdin/stdout tools;
- `MCPCapability` for an SDK-independent MCP tool transport;
- `AgentRouterCapability` as a reference adapter for the sibling router CLI.

Provider-native provenance should remain native. Return IDs/digests in `CapabilityResult.metadata`
when the control-plane audit trail should reference provider artifacts or evidence.

## Planner

A planner implements:

```python
create_plan(*, run_id, goal, version) -> Plan
revise_plan(*, run, reason, version) -> Plan
```

Planner output is untrusted input. The runtime validates run/version identity, DAG structure,
terminal success contributions, capability availability and side-effect declarations. A planner
cannot invoke tools, authorize actions, record verification, or mutate run state.

`StaticPlanner` is provided for deterministic tests and hosts that construct `Plan` objects
outside the runtime.
