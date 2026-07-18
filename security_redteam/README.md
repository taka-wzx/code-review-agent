# Week 6 deterministic security corpus

This directory contains the input-only Phase 1 registry and the Phase 3
materialized offline corpus. The mandatory runner uses generated temporary
fixtures and recording fakes only. It does not start Docker or a host process,
open a network connection, call a model, inspect host credentials, or read the
repository's existing evaluation assets.

## Integrity model

- `case-plan.json` remains the byte-for-byte preauthorization attestation. Its
  `materialized` and authorization fields intentionally remain `false`.
- `cases.jsonl` contains exactly the same 48 identities, titles, risk mappings,
  expectations, controls, and forbidden effects. Every line binds the frozen
  plan hash, deterministic seed, budgets, source commit, and its own canonical
  materialized-case hash.
- Materialization refuses to overwrite an existing corpus and verification
  rejects a missing, duplicate, reordered, altered, or extra case.
- Reports are generated artifacts, not committed results. Every rate includes
  numerator, denominator, excluded count, and exact case IDs. Synthetic
  latency uses a deterministic fake clock and is not a production benchmark.
- The A3 authorization SHA is an executable invariant: materialization,
  corpus loading, and report validation reject any other source commit.
- Reports distinguish 23 `product-code` cases (15 adversarial, 8 controls)
  from 25 `fixed-fixture` cases (21 adversarial, 4 controls), with exact IDs
  and components. A changed classification fails validation.
- Required events arise from observed policy/resource/cleanup/redaction/export
  evidence, pass through a canonical trace, and are read back before the audit
  gate can pass. The runner never stamps events from expected outcomes.

## Commands

From the Phase 3 worktree with `PYTHONPATH` explicitly set to its `src`:

```powershell
python scripts\verify_security.py --cases security_redteam\cases.jsonl
```

To generate a temporary full report for inspection:

```powershell
python scripts\verify_security.py `
  --cases security_redteam\cases.jsonl `
  --report "$env:TEMP\week6-security-report.json"
```

The committed corpus was created once from the A3 authorization anchor using
the materialization mode. Re-running materialization against the committed path
must fail rather than overwrite evidence.

## Scope of the result

Passing this suite proves deterministic control-plane regression behavior for
the frozen recording-fake cases. It does not measure resistance of a real LLM,
Docker isolation, an OTLP collector, a live exporter, or unknown attacks.
Phases 4 and 5 remain separately gated.
