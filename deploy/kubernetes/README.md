# Kubernetes Deployment Bundle

`production.template.json` is a Kubernetes JSON manifest. JSON is valid YAML,
so the rendered output can be applied with `kubectl apply -f`. The committed
template is intentionally not deployable until a release process supplies an
immutable image digest and the cluster-owned resource names.

The renderer never creates secret data. Before applying the output, a platform
operator must create the runtime ConfigMap, runtime Secret, TLS Secret, and a
ReadWriteMany storage class. The runtime ConfigMap supplies non-secret settings
such as `CRAG_DATABASE_URL` and `CRAG_REPOSITORIES_JSON`; the runtime Secret
must provide `database_password`, `webhook_secret`, and `service_token` keys.

```powershell
$python = '.venv\Scripts\python.exe'
& $python scripts/render_kubernetes_manifests.py render `
  --image registry.example.invalid/code-review-agent@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
  --ingress-host api.example.test `
  --runtime-config crag-runtime-config `
  --runtime-secret crag-runtime-secrets `
  --tls-secret api-example-test-tls `
  --artifact-storage-class rwx-storage `
  --output build\kubernetes\production.json
& $python scripts/render_kubernetes_manifests.py lint --input build\kubernetes\production.json
kubectl apply -f build\kubernetes\production.json
```

Rendering and linting are offline checks. A production rollout still requires
cluster access, a real image digest, matching secret/config resources, an RWX
storage provisioner, a TLS issuer, database migration review, and normal change
approval.
