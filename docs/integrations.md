# Integration contracts

## agent-router

`AgentRouterCapability` delegates executor selection through the router's CLI. The control plane
passes task/risk/budget context as capability input; the router owns provider/model policy. The
control plane remains authoritative for the run lifecycle, global budget and verification.

## agentic-analytics

Register its MCP tools as control-plane capabilities (source inspection, query, managed execution,
evidence/validation). Keep analytical provenance authoritative in `agentic-analytics`; return
provider evidence/artifact IDs for cross-reference from control-plane events.

## agentic-research

Expose high-level research skills/capabilities. Human methodology gates remain real human gates;
the control plane must not reinterpret a research pipeline's guidance-only step as deterministic
verification.

## agentic-vault

The runtime defines a `PersistentMemoryProvider` interface. A vault adapter may query durable
memory and promote selected `MemoryItem`s into L1 working memory. The control plane owns current
context selection, supersession and context budgets; the vault remains the durable knowledge
system.
