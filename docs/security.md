# Security model

The control plane assumes planners and capability outputs are untrusted inputs.

Enforced boundaries:

- no arbitrary host-shell tool is exposed by the core runtime;
- capability descriptors must state side effects, permissions and reversibility;
- plan side-effect declarations must match a registered provider exactly;
- high-impact mutations are human-gated by default;
- missing required verifiers block rather than pass;
- non-idempotent/ambiguous mutations are never silently retried;
- stale action approvals are bound to the exact plan version and arguments digest;
- budgets are checked before invocation and actual overruns fail the run;
- checkpoint resume validates against replayed state;
- event streams with gaps/order corruption fail closed;
- policy documents with unknown operators are rejected;
- operational feedback cannot mutate active policy or skills automatically.

Capability providers remain responsible for secret handling and provider-specific authorization.
The control-plane action ledger stores argument **digests**, not raw mutation arguments. Providers
should keep secrets out of returned output/metadata and event-safe references should be used for
sensitive artifacts.
