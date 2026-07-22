# Database migrations

Alembic is the sole schema-version authority. Run `crag-db upgrade` as a
separate deployment step; service workers only check that the database is at
the current head and never execute DDL.

Revision `0001` creates the organization-scoped production schema. Revision
`0002` imports the Week 7 `jobs` and `deliveries` tables into an isolated
`local-legacy` tenant, preserving job IDs, states, results, and delivery
idempotency records before dropping the legacy tables.
