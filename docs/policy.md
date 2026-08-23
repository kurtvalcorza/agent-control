# Policy

Policies are ordered, versioned and fail closed on unknown operators. The final rule must be an
explicit unconditional allow; there is no implicit fall-through.

```yaml
version: "2026-08-23"
policies:
  - id: destructive-write
    when:
      side_effect_class: destructive
    require:
      human_approval: true

  - id: expensive-step
    when:
      estimated_cost_usd_gt: 0.50
    require:
      justification: true
      max_estimated_cost_usd: 1.00

  - id: high-risk-analysis
    when:
      run_risk: high
    require:
      verification: [challenge]
      budget:
        max_cost_usd: 2.00
        max_tool_calls: 20

  - id: default-allow
    decision: allow
    when: {}
```

Supported `when` predicates:

- `side_effect_class`
- `run_risk`
- `capability_risk`
- `reversible`
- `estimated_cost_usd_gt`
- `permission_any`

Supported `require` controls:

- `human_approval`
- `verification`
- `justification`
- `reversible`
- `max_estimated_cost_usd`
- `budget` with `max_cost_usd`, `max_elapsed_ms`, `max_model_calls`, `max_tool_calls`

Policy budget ceilings constrain projected run usage for the action being considered. They may be
tighter than the run's original hard envelope but can never relax it.

Every decision is recorded as `PolicyEvaluated` with the policy ID, policy version, decision,
reason and imposed requirements. Unknown match/require/budget operators or malformed policy roots
are rejected.

The default policy human-gates destructive, external-communication, financial and privileged side
effects, high-risk writes, and irreversible writes.

Operational feedback never rewrites the active policy. `ProposalRegistry` can hold candidate
policy/skill changes through validation/approval/promotion states; promotion returns a reviewed
artifact rather than mutating the running engine.
