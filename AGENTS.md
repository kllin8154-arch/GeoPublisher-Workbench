# Development rules

- Read `docs/IMPLEMENTATION_CONTRACT.md` before changing interfaces.
- Ponytail: first reuse existing code and platform features; write the smallest complete diff and focused tests. Never shrink security, audit, error handling or source-data protection.
- Code, documentation, and synthetic fixture generators only. Never commit real spatial data, credentials, local environment profiles, or user attachments.
- Do not contact a production GeoServer or database during development.
- Keep network access limited to the explicitly configured endpoint; no telemetry, implicit redirects, remote styles, or shell command strings.
- Preserve source data. Initial release supports create, skip and immutable new-version only; never overwrite an existing dataset.
- A write timeout means UNKNOWN until reconciled. Journal intent before a remote write.
- A configuration snapshot is not a data backup. Never promise arbitrary replacement rollback.
- Tests should cover data integrity, retry/ownership semantics, credentials, CRS, and error detection, not merely implementation details.
- Do not edit another agent's assigned files. Coordinate interface changes before making them.
