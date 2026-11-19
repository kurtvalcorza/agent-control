# Versioning

The package/API version, event schema version, policy-document version and capability-provider
versions are independent contracts.

## Event schema

`CURRENT_EVENT_SCHEMA_VERSION` identifies the schema emitted by the runtime.
`SUPPORTED_EVENT_SCHEMA_VERSIONS` is the explicit replay compatibility set.

The current schema is `"2"`; `SUPPORTED_EVENT_SCHEMA_VERSIONS` is `{"1", "2"}`. Version `"2"`
(RFC #6 Phase A) adds the `CapabilityPreResolved` and `CapabilitySourceChecked` events and these
additive fields: `CapabilityResolved.source_provenance` / `pre_resolution_state`,
`ActionIntent.delegated`, `VerificationSpec.requires_clause_ids` and the checkpoint's
`capability_source_versions`. Version `"1"` streams replay unchanged; their checkpoints carry no
capability provenance and therefore need no source revalidation.

RFC #6 Phase B1 stays on `"2"`: it adds one additive field inside `EXTERNAL_STATE` verification
evidence (recorded in verification results and action receipts), `observation_resolution`
(`resolver`, `resolved_at`, `method`; `null` when a reference-only observation could not be
resolved). It is present only for reference-only observations; direct observations and every
existing event keep their meaning.

Every persisted event stores its schema version. Replay fails closed when an event uses an
unsupported version; it never guesses how to interpret an unknown payload.

Compatibility rules:

- additive payload fields within a supported event version must be ignored by older projection
  logic unless required for an invariant;
- semantic changes to an existing event require a new schema version;
- migrations should transform complete streams and preserve original event IDs/order in migration
  provenance rather than mutating historical events in place;
- event schema support may span more than one package release;
- dropping a supported schema version is a breaking runtime change and requires an explicit
  migration path.

## Policy documents

Policy documents carry their own free-form `version` string. Every `PolicyEvaluated` event records
both the matched policy ID and document version so historical decisions remain attributable after
policy changes.

## Capability providers and capability sources

`CapabilityDescriptor.version` describes the provider contract/version independently of package
and event versions. Plans depend on abstract capabilities; providers may evolve independently as
long as their declared schemas and safety metadata remain compatible.

Provider versions are not source provenance. The exact source that governed a run is recorded per
run as `ResolvedCapabilityProvenance` (`source_ref`, optional immutable `revision`,
`digest_algorithm="sha256"`, 64-hex `digest`), never on the process-global descriptor. See
`docs/providers.md`.
