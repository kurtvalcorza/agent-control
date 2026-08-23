# Policy

Policies are ordered and fail closed on unknown operators. The parser accepts the Issue #1
`policies/when/require` shape and the earlier `rules/match` spelling.

```yaml
policies:
  - id: destructive-write
    when:
      side_effect_class: destructive
    require:
      human_approval: true

  - id: evidence-required
    when:
      side_effect_class: reversible_write
      run_risk: high
    require:
      verification: [external_state]
      justification: true

  - id: default-allow
    decision: allow
    when: {}
```

The final rule must be an explicit unconditional allow; this prevents accidental implicit policy
fall-through. Runtime decisions are written as `PolicyEvaluated` events with policy ID, reason and
requirements.

The default policy requires explicit human approval for destructive, external-communication,
financial and privileged side effects, high-risk writes, and irreversible writes.

Policy feedback never rewrites the active policy. `ProposalRegistry` can hold a candidate change
through validate -> approve -> promote states, but promotion returns a reviewed artifact rather
than mutating the engine.
