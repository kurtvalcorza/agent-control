# Versioning and compatibility

The package version does not imply schema equivalence. Version independently:

1. public Python/CLI API;
2. event envelope/payload schemas;
3. capability descriptor schema;
4. policy schema;
5. checkpoint schema.

Current event envelopes use schema version `1`. A stable release must document any migration that
changes replay semantics before changing that version. Old event streams must either migrate
explicitly or fail with an actionable incompatibility; silent reinterpretation is forbidden.
