# Phase 11A: Synthetic Staging Deployment Validation v1

## Current status

**Synthetic staging validation succeeded for executable commit
`e901c6c4bc7a51e1572efc35690dd05df7e3b66c`. Draft PR #17 remains Draft and unmerged
because every CI job failed before receiving a runner. This is not production Ready.**

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

The owner authorized and later consumed the following bounded profile on 2026-07-28:

| Field | Recorded value |
| --- | --- |
| Target | Owner-operated Alibaba Cloud ECS `i-bp12vpivp8pdpr0uq7uf` in `cn-hangzhou` |
| Host namespace | `/opt/crag-synthetic-staging` |
| Access URL | Operator-local `http://127.0.0.1:8000` through SSH `-L 8000:<api-container-ip>:8000`; Compose publishes no host port, and container `0.0.0.0` is allowed only with the explicit trusted loopback-publication flag plus loopback-only Host/Origin allowlists; no public listener, hostname, DNS, or TLS change |
| Image path | No registry; build the frozen source on the host and record the resulting local image ID |
| Build mirrors | Defaults are `https://deb.debian.org` and `https://pypi.org/simple`; `cn-hangzhou` staging may set `CRAG_DEBIAN_MIRROR=https://mirrors.aliyun.com` and `CRAG_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`; all other values fail the Docker build |
| Database | Same-host Compose Postgres with the `postgres_data` volume; no published database port |
| Secrets | Root-owned `0600` files below `/opt/crag-synthetic-staging/secrets`, consumed only through `*_FILE` |
| Window and cost | Consumed from `2026-07-28T16:50:51+08:00` with hard expiry at `17:50:51+08:00`; CNY 0 incremental spend recorded |
| Owner | Repository and Alibaba Cloud account owner controls incident response and rollback |

The owner subsequently supplied and consumed the required bounded confirmation for
executable commit `e901c6c4bc7a51e1572efc35690dd05df7e3b66c`. It authorized one
source transfer and host-local build, reuse of the already-migrated volume without a
second migration, API/worker startup, internal-IP synthetic smoke, and isolated logical
backup/restore. It did not authorize a public endpoint, DNS/TLS change, real provider,
real repository write, paid add-on, or later redeploy.

## Stopped deployment windows

Two authorized, bounded windows on 2026-07-28 reached their build hard timeout while
downloading Debian packages from `deb.debian.org`. A third window selected the Alibaba
Debian mirror and reached the Python dependency install, but `pypi.org` downloads did
not finish before the build hard timeout. A fourth window built the application image
and migrated PostgreSQL to `0007_phase11a_repair`, then stopped when the API rejected
local-token mode on container `0.0.0.0`. The operator removed every container and
network, retained the image and two named volumes, and did not run synthetic smoke or
backup/restore. These are failure receipts, not staging-validation evidence.

A fifth window confirmed that the loopback-publication fix allowed the API to become
healthy. Both worker main processes remained running, but Docker killed each health
probe at the fixed five-second timeout before its database-backed check completed. The
operator removed every container and network, retained the image and two named volumes,
and did not run synthetic smoke or backup/restore. The approved offline remediation
raises only the Review and Repair worker health-probe timeout from 5 to 30 seconds; API
and PostgreSQL health budgets remain unchanged.

A sixth window made PostgreSQL, API, Review worker, and Repair worker healthy. Docker
did not install the requested HostPort for the API on the internal-only network, so host
`127.0.0.1:8000` refused the smoke connection even though the API was healthy inside
the container. The operator removed every container and network, retained the image and
two named volumes, and created no synthetic job. The approved remediation removes all
Compose HostPort declarations and uses SSH local forwarding directly to the dynamic API
container IP, preserving the internal network and its lack of a default route.

The seventh window completed staging validation for executable commit `e901c6c...`.
The source archive, authorization record, rendered Compose configuration, and local
application image are bound to SHA-256
`16c206611b81fca899857a95323070806f1ef9f43ee1d4e862fde962d468c434`, SHA-256
`ec46f250f9bf10371371998bfc8862042efe4de3f7bcc08601e1d7c03621ead0`, SHA-256
`a9a9195f9bead6ed55db4b91c51e878613c23d904d738cf74b8dba208aa7cdf4`, and image ID
`sha256:83e3274b2af08b9482dae1ae7158a93a94191259f010adc37afb5002e57da7b0`.
All four containers were healthy, no HostPort or host listener existed on 8000/5432,
and workload processes ran as UID 1000 with zero effective capabilities, read-only root
filesystems, and `no-new-privileges`. The synthetic Repair flow reached
`draft_published` through both exact approvals. Backup/restore matched Alembic head
`0007_phase11a_repair`, 39 table names, and every exact row count; the verification
database and raw dump were deleted. The root-owned `0600` success receipt has SHA-256
`a372663657d12df636399de8d69de06dd7ca5bdd6a4e3d4380cdde839902c4a1`.

The approved remediation keeps the fail-closed `DEBIAN_MIRROR` build argument and adds
a fail-closed `PYPI_INDEX_URL` argument. Dockerfiles default to the upstream Debian and
PyPI endpoints and allow only those endpoints or Alibaba Cloud's corresponding mirrors.
The staging environment must explicitly select both Alibaba values through
`CRAG_DEBIAN_MIRROR` and `CRAG_PYPI_INDEX_URL`; arbitrary mirrors are rejected. The
endpoints are listed in the [Alibaba Cloud Debian mirror documentation](https://developer.aliyun.com/mirror/debian)
and [Alibaba Cloud PyPI mirror documentation](https://developer.aliyun.com/mirror/pypi).
The approved loopback-publication remediation adds an explicit
`CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH` gate. It permits only container `0.0.0.0`,
requires local-token mode plus loopback-only Host/Origin allowlists, and remains paired
with Compose publication on host `127.0.0.1`. The default remains disabled. This code
change invalidates the previous source archive and rendered Compose hash, so all freeze
evidence must be regenerated before another deployment window.

## Prepared deployment sequence

The following sequence is the frozen runbook used by the consumed seventh-window
authorization. Repeating it requires a new explicit confirmation:

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
   `CRAG_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`, and enable
   `CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH=true` only for the loopback-published API.
   Render `docker compose config` and reject any non-allowlisted mirror, provider key,
   GitHub writer, real repository mount, Docker HostPort declaration, non-loopback
   Host/Origin allowlist, or database publication.
5. Pull only the existing public base images required by the frozen Dockerfiles, build
   the local service image, record its immutable local image ID, and record the rendered
   deployment-configuration SHA-256. Any billable registry or add-on aborts the window.
6. Start Postgres alone, run the explicit `migrate` profile exactly once, record the
   migration result, and start API/Review worker/synthetic Repair worker only after the
   exact Alembic head check passes.
7. Verify container hardening, loopback-only publication, database and migration
   readiness, API health/readiness, worker heartbeat, fake-only adapters, zero real model
   calls, zero real repository writes, and redacted metrics/receipts.
8. Resolve the running API container's internal IP with `docker inspect`, then access it
   only through operator SSH `-L 8000:<api-container-ip>:8000` and send loopback Host
   headers. No Docker HostPort, HTTP, HTTPS, database, or management port is opened on
   the ECS host or to the Internet.
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

The consumed confirmation explicitly named the bounded 60-minute zero-cost window,
tracked-source transfer, public-base build, reused migrated volume, API/worker start,
internal-IP synthetic smoke, and backup/restore. It expressly prohibited rerunning the
migration. That confirmation is exhausted; a generic request to continue is not new
deployment authority.
