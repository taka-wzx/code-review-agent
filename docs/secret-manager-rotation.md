# Runtime provider-secret injection and rotation

Durable Review workers can load a provider API key from one versioned JSON file
rendered by a secret manager or sidecar. The worker validates and builds the
initial client before startup completes, then reloads the file before every job.
A higher generation therefore takes effect without restarting the worker.

This adapter rotates only the LLM provider credential. Database connection-pool
credential rotation is a separate deployment and engine-lifecycle concern.

## Worker configuration

Set the provider and the absolute path to the rendered file:

```text
LLM_PROVIDER=glm
LLM_MODEL=glm-4.6
CRAG_PROVIDER_SECRET_FILE=C:\run\secrets\crag-provider.json
```

`LLM_PROVIDER` is `deepseek` or `glm`. The provider and model are frozen when
the worker starts. `CRAG_PROVIDER_SECRET_FILE` is removed from `os.environ`
after a successful preflight, while the adapter retains its private `Path`
object.

Do not set any of these legacy variables in secret-manager mode:

```text
DEEPSEEK_API_KEY
DEEPSEEK_API_KEY_FILE
GLM_API_KEY
GLM_API_KEY_FILE
ZHIPUAI_API_KEY
ZHIPUAI_API_KEY_FILE
```

Mixed configuration is rejected. When `CRAG_PROVIDER_SECRET_FILE` is absent,
the existing durable-worker `*_API_KEY_FILE` behavior remains unchanged.

## Rendered file contract

The file is UTF-8 JSON with exactly these fields:

```json
{
  "schema_version": "crag.runtime-secret/v1",
  "secret_id": "crag.provider.glm.api-key",
  "version": "provider-key-2026-08-08",
  "generation": 17,
  "value": "<injected-provider-api-key>",
  "not_before_utc": "2026-08-08T11:55:00Z",
  "expires_at_utc": "2026-08-08T13:00:00Z"
}
```

The expected `secret_id` is tied to `LLM_PROVIDER`:

| Provider | Required `secret_id` |
| --- | --- |
| `deepseek` | `crag.provider.deepseek.api-key` |
| `glm` | `crag.provider.glm.api-key` |

`version` is a non-secret identifier. `generation` starts at 1 and must increase
for every material or version change. Timestamps use UTC whole-second RFC 3339
form. The credential must already be valid and have at least 60 seconds of
remaining validity whenever a job starts.

The JSON file is capped at 16 KiB and the credential value at 4 KiB. The adapter
requires one regular, non-symlink file. On POSIX, group/other write permissions
are rejected; use mode `0600` or a stricter equivalent. On Windows, the
deployment must restrict the file DACL to the injector and worker identities.
The adapter does not serialize the path or value.

## Rotation procedure

1. Keep the currently active provider credential valid for in-flight jobs.
2. Render the complete next payload to a staging file in the same directory and
   filesystem as the active file.
3. Apply the final ownership and permissions to the staging file.
4. Atomically replace the active path with the staging file. Do not truncate or
   rewrite the active inode in place.
5. Wait for a `rotated` event for the new generation before revoking the prior
   credential. Account for the longest permitted in-flight job when choosing the
   overlap window.

An unchanged generation reuses the existing client. A higher generation builds
one replacement client under a process lock and swaps it atomically. Jobs that
already acquired the old client can finish with it; later jobs receive the new
client. A rollback, same-generation conflict, invalid/expired payload, or client
construction failure rejects the affected new job instead of returning the
cached client.

The process reserves the generation and version/material fingerprints when it
first observes a candidate, before client construction. If construction fails,
the exact same candidate may be retried. Replacing it with a lower generation or
different content under the same generation is rejected; publish a higher
generation to recover from an incorrectly rendered candidate.

## Telemetry and failure states

Each load attempt emits only this bounded shape through the worker logger:

```json
{
  "schema_version": "crag.secret-rotation-event/v1",
  "status": "rotated",
  "generation": 17,
  "version_sha256": "<64 lowercase hex characters>",
  "failure_code": null,
  "observed_at_utc": "2026-08-08T12:00:00Z",
  "secret_value_retained": false,
  "secret_path_retained": false
}
```

Status is `loaded`, `unchanged`, `rotated`, or `failed`. Failures expose one of
these codes, never the file path, credential, provider response, or raw
exception text:

| Failure code | Meaning |
| --- | --- |
| `secret_source_unavailable` | The configured source cannot be opened/read. |
| `secret_file_denied` | The path is not an allowed regular file or has unsafe permissions. |
| `secret_file_oversized` | The rendered file exceeds 16 KiB. |
| `secret_payload_invalid` | JSON, fields, value, or timestamps violate the contract. |
| `secret_identity_mismatch` | `secret_id` does not match the frozen provider. |
| `secret_not_yet_valid` | The validity window has not started. |
| `secret_expired` | The validity window has ended. |
| `secret_expires_too_soon` | Less than 60 seconds of validity remains. |
| `secret_generation_rollback` | The rendered generation is below the highest generation observed by this process. |
| `secret_generation_conflict` | Version/material differs from the first payload observed at this generation. |
| `secret_client_build_failed` | The SDK client rejected the new material/configuration. |

Treat repeated `failed` events as an injector or rotation incident. Repair the
rendered payload with a generation greater than the last successfully active
generation; do not lower the generation or restore old material under a reused
generation.
