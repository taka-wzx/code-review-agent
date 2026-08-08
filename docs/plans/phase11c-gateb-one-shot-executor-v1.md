# Phase 11C Gate B One-Shot Synthetic Executor v1

Status: **local implementation and offline validation complete; ECS source transfer and
deployment freeze remain pending, and provider dispatch requires the final exact
approval binding**

## Identity and authority

- Task branch: `codex/phase11c-gateb-one-shot-executor-v1`
- Task worktree: `.codex-worktrees/phase11c-gateb-one-shot-executor-v1`
- Base commit: `a6bdf664e4f76bc5e67542dc8decb774ffae0364`
- Owner and kill-switch operator: `taka-wzx`
- Aggregate hard ceiling: `15,000,000` micro-CNY
- Credential mode: fixed Linux ECS file `/run/crag-gateb/glm_api_key`
- Accepted credential fingerprint:
  `0f2b300e874ecbd0c4d14b9d5b5d381e601fae658d14c400540cd1533581d6ff`
- Accepted observed tariff evidence SHA-256:
  `cafc2a706e0d1da40d79db2e4f464df75db42b06240812e164a75f532043e70c`
- Tariff observed locally at `2026-07-30T09:26:31Z`; the owner explicitly waived a
  separately documented historical effective date for this one-call diagnostic.

The user explicitly requested completion of the remaining Gate B work and accepted a
single paid deterministic synthetic diagnostic under the existing CNY 15 aggregate
ceiling.  This task may implement a real credential reader and HTTPS transport, prepare
deployment bindings, and create a local task-branch commit.  It must not dispatch until
the exact final authorization, image/deployment/runtime bindings, future UTC window,
and one-use approval text have all been shown and validated.

## Complete Single Writer declaration

Codex is the sole writer for exactly these new files:

- `docs/plans/phase11c-gateb-one-shot-executor-v1.md`;
- `phase11c_gateb_one_shot_executor.py`;
- `schemas/phase11c-gateb-one-shot-authorization.schema.json`;
- `schemas/phase11c-gateb-one-shot-receipt.schema.json`;
- `Dockerfile.phase11c-gateb`;
- `compose.phase11c-gateb.yml`;
- `tests/test_phase11c_gateb_one_shot_executor.py`.

Every other path is read-only.  In particular, prior Gate A/B artifacts, `src/`,
dependencies and lockfiles, workflows, migrations, product Compose files, and every
`eval/**` asset are prohibited.

## Frozen diagnostic

- Provider/model: standard GLM API / `glm-5.2`.
- Endpoint: `https://open.bigmodel.cn/api/paas/v4/chat/completions`.
- Exactly one logical call and one HTTP attempt; no retry and no redirect.
- Direct TLS with the system trust store; environment proxy inheritance is not used.
- Fixed non-streaming request, `temperature=0.01`, thinking disabled, and
  `max_tokens=128`.
- Fixed synthetic prompt asks for exactly `PHASE11C_GATEB_OK`; no source code, personal
  data, repository data, tool, browsing, or external write capability is present.
- Maximum accounting input is 2,000 tokens.  At observed rates of CNY 8/million input
  and CNY 28/million output, the exact worst-case reservation is 19,584 micro-CNY
  (CNY 0.019584).  Cached input is never assumed for reservation.

## Fail-closed execution order

1. Parse and validate the sealed final authorization against the actual source,
   request, endpoint, tariff evidence, credential fingerprint, and current UTC time.
2. Validate the exact one-use approval text.
3. Under an exclusive Linux file lock, durably record approval consumption.
4. Durably reserve the full worst-case budget.
5. Open the fixed credential with `O_NOFOLLOW`, verify root ownership, exact `0600`,
   one link, bounded nonzero size, repository-external absolute path, and fingerprint.
6. Durably record credential validation.
7. Durably record the sole HTTP attempt before opening the TLS connection.
8. Send the fixed request once, read a bounded response, discard raw content, map all
   failures to stable enums, and write a sealed safe receipt.
9. Never roll back approval, budget, credential, or attempt state.  Re-execution for
   the same state directory is refused even after a crash or provider failure.

## Receipt boundary

Receipts and durable state contain only hashes, enums, booleans, and nonnegative
integers.  They never retain the API key, prompt text, response text, authorization
header, exception message, host path, hostname, account ID, or raw provider payload.
The response is accepted only when the assistant content equals the frozen terminal
token after surrounding whitespace removal.  Any other content is `inconclusive`.

## Frozen executable interface

- `print-template` prints the current executable/request/endpoint/cohort bindings and
  explicit `PENDING_FREEZE` placeholders; it does not read a credential or open a
  socket.
- `seal-authorization` accepts one bounded candidate document on standard input,
  validates every exact field and the no-longer-than-30-minute UTC window, and emits
  the sealed authorization on standard output.
- `print-approval-binding` reads only the fixed
  `/run/crag-gateb/authorization.json` control file and prints the exact one-use
  approval text; it does not read the credential or open a socket.
- `run` is the sole live command.  It reads fixed root-owned `0400` authorization and
  approval files, the fixed root-owned `0600` credential, and the fixed persistent
  `/var/lib/crag-gateb` state directory.  It accepts no endpoint, prompt, credential
  path, retry, model, token, budget, or state-path argument.
- The approval file has no trailing newline or BOM.  Its only valid content is
  `APPROVE PHASE11C DIAGNOSTIC <approval_binding_sha256>`.

The Compose service uses a required `PHASE11C_GATEB_IMAGE` value so the final run can
name the exact local `sha256:<image-id>` instead of a mutable tag.  It has a read-only
root filesystem, all capabilities dropped, `no-new-privileges`, bounded CPU/memory/
PIDs, no published ports, three fixed read-only control/credential mounts, and one
dedicated persistent state volume.  Deployment must use a filtered directory
containing only the approved task artifacts; the repository root is not an approved
Docker build context.

## Offline validation

Tests must use fakes only and must never open the real credential or a network socket.
They cover strict schemas and seals, window/binding drift, exact approval, monotonic
state transitions, negative/bool/float accounting rejection, credential metadata and
TOCTOU checks, one-attempt behavior, bounded response parsing, stable failure mapping,
receipt redaction, and AST guards on transport/persistence primitives.

Required commands:

```powershell
$Python = (Resolve-Path '..\\..\\.venv\\Scripts\\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase11c_gateb_one_shot_executor
& $Python phase11c_gateb_one_shot_executor.py print-template
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py phase11c_gateb_live_diagnostic.py phase11c_gateb_one_shot_executor.py
& $Python scripts/verify.py
& $Python -m pip check
git diff --cached --check
```

No command above may read `eval/**`, the real credential, provider APIs, cloud control
planes, or GitHub write APIs.  Pushing, ECS mutation, image building, deployment, and
the one paid dispatch are separately reported actions and must preserve the exact
binding/approval sequence.
