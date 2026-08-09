# Repository Feedback Rules

Repository feedback rules are immutable, versioned administrator configuration
that can influence subsequent review evaluation. They are separate from
organization policy and from repository-memory entries.

## Lifecycle

1. An organization administrator creates a version containing 1-64 validated
   rules. Repeating the same version and canonical content is idempotent;
   reusing the version with different content is rejected.
2. Activation atomically replaces the repository active pointer, increments a
   monotonic generation, and inserts a SHA-256-addressed receipt in the same
   database transaction.
3. Rollback may target only a version that was active previously. It is another
   monotonic transition, not a deletion or mutation of later versions.
4. Review submission copies the active version, generation, canonical rules,
   and rules hash into a job binding. The logical submission identity also
   includes this rule identity, so a new active generation cannot deduplicate to
   a review created under an older generation.
5. Workers use only the job binding. They do not re-read the repository active
   pointer, so activation or rollback cannot change an in-flight evaluation.

## Rule Shape

Each rule has these fields:

- `rule_id`: unique within the version, up to 64 identifier characters.
- `category`: a lowercase bounded category.
- `action`: `prioritize`, `suppress`, or `require_verification`.
- `condition`: the repository-specific condition to evaluate.
- `rationale`: why maintainers accepted the feedback pattern.

The canonical JSON document is limited to 32 KiB. Its SHA-256 is stored on the
version, active receipt, and review binding.

## API

All paths are scoped under
`/v1/organizations/{organization_id}/repositories/{repository_id}`.

- `GET /feedback-rules`: list immutable versions.
- `POST /feedback-rules`: create one version.
- `GET /feedback-rules/active`: read the active binding.
- `POST /feedback-rules/{version}/activate`: activate a version.
- `POST /feedback-rules/{version}/rollback`: restore a previously active version.
- `GET /feedback-rule-receipts`: list immutable transition receipts.

Repository readers may use the GET endpoints when they have repository access.
Only principals with `manage_policy` may create or transition versions. The
existing API organization and repository authorization remains authoritative.

## Migration Dependency

Migration `0011_issue34_feedback_rules` follows
`0010_issue33_github_webhook`. Deploy PR #51 before this change, run the normal
explicit migration step, and then deploy API and workers from the same release.
