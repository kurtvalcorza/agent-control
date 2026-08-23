# Versioning

The package/API version, event schema version, policy-document version and capability-provider
versions are independent contracts.

## Event schema

`CURRENT_EVENT_SCHEMA_VERSION` identifies the schema emitted by the runtime.
`SUPPORTED_EVENT_SCHEMA_VERSIONS` is the explicit replay compatibility set.

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

## Capability providers

`CapabilityDescriptor.version` describes the provider contract/version independently of package
and event versions. Plans depend on abstract capabilities; providers may evolve independently as
long as their declared schemas and safety metadata remain compatible.
