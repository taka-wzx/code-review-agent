# Issue #36: Cloud deployment manifests

## Goal

Provide a renderable Kubernetes deployment bundle for the API, worker, and
database migration job. The bundle must terminate TLS at an Ingress, expose
only health-checked workloads, set explicit resource requests and limits, and
keep deployment configuration and secrets outside version control.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-36-cloud-deployment`

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue36-cloud-deployment.md`, `deploy/kubernetes/production.template.json`, `deploy/kubernetes/README.md`, `scripts/render_kubernetes_manifests.py`, `tests/test_issue36_cloud_deployment.py` | all other paths |

## Frozen interfaces

- The committed bundle is a JSON Kubernetes template. JSON is valid YAML and
  can be passed directly to `kubectl apply -f` after rendering.
- Rendering requires an immutable image digest, cluster hostname, existing
  runtime ConfigMap, existing runtime Secret, existing TLS Secret, and an RWX
  storage class. No defaults can silently select mutable image tags or create
  secret contents.
- API probes use the existing `/healthz` and `/readyz` endpoints. Worker probes
  use the existing `crag-worker --check` command.
- The renderer and linter use only the Python standard library and never call
  a cluster, registry, cloud API, or secret manager.

## Prohibited changes

- No changes to application source, dependencies, CI workflows, Dockerfiles,
  Compose assets, `eval/**`, or `eval/holdout/**`.
- No real registry digest, cloud endpoint, database URL, certificate, token,
  private key, secret value, or kubeconfig in committed files or tests.
- No claim that rendering alone performs a production deployment.

## Implementation

- Add a Kubernetes `List` template containing Namespace, ServiceAccount,
  non-secret defaults ConfigMap, RWX PVC, API Deployment and Service, Worker
  Deployment, migration Job, NetworkPolicy, PodDisruptionBudget, and TLS
  Ingress.
- Set restricted pod/container security context, health probes, rolling-update
  strategy, resource requests/limits, availability controls, and secret file
  mounts.
- Add an explicit renderer that validates all supplied deployment identifiers
  and immutable image digests before substitution.
- Add a linter that rejects unresolved placeholders, mutable/tampered images,
  missing TLS/probes/resources, unsafe security contexts, absent configuration
  separation, and malformed resource shape.
- Add offline tests for successful rendering and linting plus invalid image,
  TLS, and probe failure cases.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue36_cloud_deployment
& $python -m ruff check scripts/render_kubernetes_manifests.py tests/test_issue36_cloud_deployment.py
& $python scripts/render_kubernetes_manifests.py --help
git diff --check
```

## Acceptance criteria

- A valid immutable image digest renders a syntactically valid Kubernetes bundle
  and passes the offline linter.
- The rendered API and worker workloads have readiness/liveness probes and
  explicit CPU/memory requests and limits.
- Ingress has a validated TLS host and secret binding; no plaintext Secret
  resource or secret material is committed.
- Invalid or tampered image references and missing TLS/probe controls fail
  validation before any deployment action.
