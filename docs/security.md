# Security model

The control plane treats planner output, capability output, transport errors, external state and
policy documents as untrusted inputs.

Enforced boundaries:

- no arbitrary shell is exposed by the runtime; subprocess adapters use `shell=False`;
- capability descriptors declare side effects, permissions, reversibility and risk;
- the host supplies the granted permission set and maximum capability risk; planners cannot expand
  that authorization envelope;
- plan side-effect declarations must match an authorized registered provider exactly;
- high-impact mutations are human-gated by default;
- missing required verifiers block rather than pass;
- non-idempotent ambiguous mutations are never silently retried;
- stale approvals are bound to the plan version and action argument digest;
- budgets are checked before invocation and reserved before execution;
- checkpoint resume validates replayed state, unresolved actions, budget reservations, memory and
  external-resource versions;
- event streams with gaps, duplicate IDs, mixed run IDs, invalid timestamps or unsupported schema
  versions fail closed;
- policy documents with unknown operators fail closed and policy decisions record policy version;
- operational feedback may propose policy/skill changes but cannot self-promote them.

## Secret handling

Raw credentials must not be embedded in `PlanNode.inputs`. Credential-shaped fields accept opaque
references such as `secret://...`, `env://...` and `vault://...`; literal values are rejected before
a plan is accepted.

All event payloads pass through the audit redactor. Sensitive keys are redacted unless they contain
an allowed opaque reference, and common credential-shaped text in exception/error strings is
scrubbed before persistence. Mutation arguments are represented in the action ledger by digest,
not by a second raw argument copy.

Capability providers remain responsible for resolving secret references and enforcing
provider-native authorization. Provider outputs and metadata should return durable IDs/digests for
artifacts and provenance rather than credential-bearing content.

## Trust hierarchy

The runtime is authoritative for state transitions, policy decisions, budget accounting,
authorization, persistence and verification status. Planners may propose plans. Capability
providers may perform domain work. Neither can manufacture a successful runtime transition or
bypass a required gate.
