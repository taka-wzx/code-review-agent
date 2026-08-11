# Phase 11D Pilot v2 Candidate 08: Trace Span Tokens

## Scope

Add regression coverage for bounded trace span token normalization while preserving
supported model namespace separators.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c08-trace-span-tokens.md`
- `tests/test_phase11d_pilot_v2_c08_trace_span_tokens.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Unsupported characters and whitespace are replaced in span tokens.
- Supported `/` namespace separators remain available for provider/model names.
- Empty normalized values use the fallback.
- Output remains bounded to 96 characters.
