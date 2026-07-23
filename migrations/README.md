# Database migrations

Alembic is the sole schema-version authority. Run `crag-db upgrade` as a
separate deployment step; service workers only check that the database is at
the current head and never execute DDL.

Revision `0001` creates the organization-scoped production schema. Revision
`0002` imports the Week 7 `jobs` and `deliveries` tables into an isolated
`local-legacy` tenant, preserving job IDs, states, results, and delivery
idempotency records before dropping the legacy tables.

Revision `0003` upgrades `review_jobs` to the Phase 9C durable lifecycle. It
adds idempotency fingerprints, payload/trace keys, Postgres-compatible lease
and heartbeat timestamps, fencing tokens, attempt/model-call counters, queue
claim indexes, organization/repository quota rows, worker heartbeats, and one
submission event per logical job. The `review_idempotency_keys` mapping allows
multiple late-arriving REST/Webhook keys to bind to the same logical job while
keeping `(organization_id, key_hash)` unique and retaining only key digests.
Historical `succeeded` jobs become `awaiting_approval`. Historical
`pull_request` jobs in `running` return to `queued` so that a new worker must
obtain a fresh lease before executing them. Phase 9B did not persist inline
diff payloads, so nonterminal rows whose `source_kind` is not `pull_request`
fail closed as `legacy_payload_unavailable` instead of being mis-executed as a
PR. Historical rows receive deterministic `legacy:<job-id>` submission keys
and retain their source hash as the request fingerprint.

Production rollback of `0003` uses the pre-migration database backup. Its
Alembic downgrade is deliberately lossy and exists only for empty-database or
test rehearsal: expanded states collapse back to the Phase 9B four-state
model, and lease, quota, worker, attempt, and submission-event history is
discarded.
