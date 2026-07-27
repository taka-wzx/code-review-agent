# Phase 11A: Synthetic Staging Deployment Validation v1

## Current status

**Offline implementation and local synthetic validation only. Not deployed. Not Ready.**

This phase retains the immutable declarations below in every synthetic response and
receipt:

```text
environment=synthetic_staging
synthetic_only=true
real_model_calls=false
real_repository_writes=false
business_claim_allowed=false
quality_claim_allowed=false
production_ready=false
```

The auth-004 outcome is not touched: five selected/headline attempts remain failed,
the stable aggregate is `provider_or_pipeline_RuntimeError × 5`, and the root cause is
still `unknown`. Phase 11A synthetic activity is not a rerun, replacement, denominator
change, success backfill, Business Pilot, quality result, or production claim.

## What the offline implementation adds

- A PostgreSQL-only-at-runtime Repair store with checkpoint history, version/CAS state
  transitions, durable budget/reservations, issued/consumed approvals, operation
  intents, redacted receipts, outbox state, lease metadata, and worker heartbeats.
- An authenticated `/v1/repairs` API for creation, status, WRITE/DRAFT_PR approval
  views and decisions, and redacted receipts. Only same-organization maintainer or
  org-admin identities can create or decide; cross-organization objects are hidden.
- Expiring, one-use approval IDs bound to the exact checkpoint and current immutable
  plan/diff/test/budget/publisher bindings. Replay, expiry, mismatch, stale state, and
  concurrent double decision fail closed.
- A separate `crag-worker --synthetic-repair` worker that uses only deterministic
  offline planner/executor adapters and Fake/DryRun Draft PR publishing. Real provider
  configuration, GitHub writer configuration, provider credentials, merge APIs, and
  real repository adapters are startup-denied.
- Compose separation for PostgreSQL, explicit migration, API, Review worker, and
  synthetic Repair worker. The runtime network is internal; no provider secret is
  declared in the Compose model.
- A sandbox image that runs as non-root. The local container validation uses a
  read-only filesystem, all capabilities dropped, a tmpfs `/tmp`, and `network=none`.

Private synthetic diff content remains inside the private checkpoint only. It is never
written to audit events, receipts, metrics, or traces, and is returned only in the
authorized DRAFT_PR approval view.

## Local, non-staging evidence

The following offline checks were completed against synthetic data only:

- 30 complete synthetic Repair jobs, with zero real model calls and zero real repository
  writes;
- one-winner concurrent approval and stale/replay/mismatch/expiry rejection;
- forced restart boundaries after plan receipt, mutation intent, and publish intent;
  recovery made zero duplicate model/mutation/commit/publication calls, and unresolved
  intent entered quarantine;
- real local Postgres migration, transaction/CAS/outbox/recovery validation;
- logical backup and restore of the local ephemeral Postgres test database followed by
  checkpoint/budget/approval consistency validation;
- local Repair sandbox build plus non-root, `network=none`, read-only, capability-drop
  smoke test.

These are local engineering checks. They are not a staging deployment, a cloud backup
exercise, an image registry publication, a TLS check, a real identity verification, or
a production readiness claim.

## Deployment gate and required handoff

The task currently lacks the staging account/project, region/namespace, hostname,
registry, database/volume, permitted API/image/migration/backup operations, DNS/TLS
authority, deployment window, cost cap, incident owner, and approved secret injection
method. Therefore do not:

- deploy API/worker/Postgres/migration services;
- push an image, record an immutable registry digest, or alter DNS/TLS;
- run a staging migration or staging backup/restore drill;
- transition a pull request to Ready or report Phase 11A completed.

When those values are supplied, the operator must first freeze and record the code
commit SHA, immutable image digest, authorization SHA, and deployment configuration SHA.
Then run the explicit migration job before API/worker startup, inject secrets only via a
secret manager or `*_FILE`, verify TLS and real maintainer/admin identity, verify
health/readiness independently for process/database/migration/worker, perform the
authorized backup/restore drill, and retain only a redacted deployment report with its
SHA-256. Any executable-code change after external validation invalidates that result.
