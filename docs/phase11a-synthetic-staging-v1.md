# Phase 11A: Synthetic Staging Deployment Validation v1

## Current status

**Zero-incremental-cost staging preparation is authorized. Deployment execution still
requires a separate explicit confirmation. Not deployed. Not Ready.**

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

The owner authorized the following preparation profile on 2026-07-28:

| Field | Recorded value |
| --- | --- |
| Target | Owner-operated Alibaba Cloud ECS `i-bp12vpivp8pdpr0uq7uf` in `cn-hangzhou` |
| Host namespace | `/opt/crag-synthetic-staging` |
| Access URL | `http://127.0.0.1:8000` through an SSH tunnel; no public listener, hostname, DNS, or TLS change |
| Image path | No registry; build the frozen source on the host and record the resulting local image ID |
| Build mirrors | Defaults are `https://deb.debian.org` and `https://pypi.org/simple`; `cn-hangzhou` staging may set `CRAG_DEBIAN_MIRROR=https://mirrors.aliyun.com` and `CRAG_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`; all other values fail the Docker build |
| Database | Same-host Compose Postgres with the `postgres_data` volume; no published database port |
| Secrets | Root-owned `0600` files below `/opt/crag-synthetic-staging/secrets`, consumed only through `*_FILE` |
| Window and cost | At most 60 minutes after a separate deployment confirmation; CNY 0 incremental spend |
| Owner | Repository and Alibaba Cloud account owner controls incident response and rollback |

This authorization permits documentation and deployment preparation only. Therefore,
until the owner gives the required second confirmation, do not:

- connect to or mutate the staging host, transfer source, or pull/build an image;
- deploy API/worker/Postgres/migration services or record a local image ID;
- push an image or alter DNS/TLS;
- run a staging migration or staging backup/restore drill;
- transition a pull request to Ready or report Phase 11A completed.

## Stopped deployment windows

Two authorized, bounded windows on 2026-07-28 reached their build hard timeout while
downloading Debian packages from `deb.debian.org`. A third window selected the Alibaba
Debian mirror and reached the Python dependency install, but `pypi.org` downloads did
not finish before the build hard timeout. Every attempt stopped before creating the
application image. Final audits reported zero Compose containers, volumes, and networks
and confirmed that migration, services, synthetic smoke, and backup/restore had not run.
These are failure receipts, not staging-validation evidence.

The approved remediation keeps the fail-closed `DEBIAN_MIRROR` build argument and adds
a fail-closed `PYPI_INDEX_URL` argument. Dockerfiles default to the upstream Debian and
PyPI endpoints and allow only those endpoints or Alibaba Cloud's corresponding mirrors.
The staging environment must explicitly select both Alibaba values through
`CRAG_DEBIAN_MIRROR` and `CRAG_PYPI_INDEX_URL`; arbitrary mirrors are rejected. The
endpoints are listed in the [Alibaba Cloud Debian mirror documentation](https://developer.aliyun.com/mirror/debian)
and [Alibaba Cloud PyPI mirror documentation](https://developer.aliyun.com/mirror/pypi).
This code change invalidates the previous source archive and rendered Compose hash, so
all freeze evidence must be regenerated before another deployment window.

## Prepared deployment sequence

The following sequence is documentation only and must not be executed before the second
confirmation:

1. Confirm the ECS identity and `cn-hangzhou` region, confirm Docker/Compose health,
   confirm that SSH 22 is restricted to the operator's current `/32`, and confirm that
   ports 8000 and 5432 are not present in any public security-group rule.
2. Require a clean task worktree, freeze the exact commit SHA, create a tracked-files-only
   archive with `git archive`, calculate its SHA-256 locally, transfer it into a new
   `/opt/crag-synthetic-staging/incoming` directory, and verify the same hash remotely.
3. Create `/opt/crag-synthetic-staging/secrets` as root with mode `0700`. Generate unique
   Postgres, webhook, and service-token values directly on the host as `0600` files.
   Never print, download, or paste those values.
4. Set only non-secret Compose paths and the filtered build context in a root-owned
   environment file. For `cn-hangzhou`, set
   `CRAG_DEBIAN_MIRROR=https://mirrors.aliyun.com` and
   `CRAG_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`. Render
   `docker compose config` and reject any non-allowlisted mirror, provider key, GitHub
   writer, real repository mount, non-loopback API publication, or database publication.
5. Pull only the existing public base images required by the frozen Dockerfiles, build
   the local service image, record its immutable local image ID, and record the rendered
   deployment-configuration SHA-256. Any billable registry or add-on aborts the window.
6. Start Postgres alone, run the explicit `migrate` profile exactly once, record the
   migration result, and start API/Review worker/synthetic Repair worker only after the
   exact Alembic head check passes.
7. Verify container hardening, loopback-only publication, database and migration
   readiness, API health/readiness, worker heartbeat, fake-only adapters, zero real model
   calls, zero real repository writes, and redacted metrics/receipts.
8. Access the API only with an SSH local-forward to `127.0.0.1:8000`. No HTTP, HTTPS,
   database, or management port is opened to the Internet.
9. After separate backup/restore confirmation within the same window, create a local
   logical backup, restore it into an isolated verification database, compare bounded
   consistency evidence, and retain only the redacted report hash.
10. On any failed gate, stop API/workers, preserve the database volume and redacted logs,
    record the failure, and leave the public security group unchanged. Do not claim a
    successful deployment.

Before execution the operator must still record the frozen commit SHA, authorization
record SHA, rendered deployment-configuration SHA, and then the local image ID. Any
executable-code change after validation invalidates the result. The zero-cost, no-DNS,
SSH-tunnel profile deliberately does not provide public TLS or production-readiness
evidence.

The second confirmation must explicitly authorize the bounded 60-minute window and the
following mutations: tracked-source transfer, public base-image pulls, local image
build, Postgres volume creation/start, explicit schema migration, API/worker start, and
synthetic smoke validation. Backup/restore remains excluded unless that confirmation
names it. A generic request to continue is not deployment authority.
