"""Rule-firing sentinel: pattern-gated rescue of verifier drops (W13-W15).

WHAT THIS IS. The verifier's system prompt contains Evidence rules that
forbid specific reasoning when dropping a finding (e.g. "a future caller
could pass X" against a config-disabled dead path). LLMs violate their own
rules intermittently. This module is the code-level backstop: a dropped
finding whose drop_reason uses forbidden reasoning AND whose issue is of
the class the rule protects is demoted to the *uncertain* channel (never
silently kept) instead of dying.

WHY IT EXISTS (design rationale). Five historical true-bug kills were
recorded across eval generations (w10r3-d6, w11r3-d5, W14 slice r1-d6,
W14 full r3-d5, w10r3 d7-gap-connect) where the verifier's stated
drop_reason quoted, near-verbatim, reasoning the prompt already forbids.
Prompt-only fixes were tried first and did not hold across runs; the
sentinel encodes the *rule*, conjunctively (reason gate AND issue gate),
not the individual cases.

HOW IT WAS VALIDATED. Offline sweeps over every recorded drop in six
results directories (replay_verifier.py --sweep): all five historical
true-bug kills match, zero false rescues among the legitimately dropped
findings (mechanism refutations, no-callers reverse-rule drops,
duplicates). Live checks: bench_verifier 30/30; a W15 replay in which a
fresh verifier re-killed d5-deadzone and the sentinel rescued it (judge
scored HIT: +1 true bug / 0 FP / 0 noise). Negative controls are frozen
as unit tests (tests/test_pure.py) -- e.g. "does not document that" (a
missing-doc nit in citation clothing) must NOT rescue.

KNOWN LIMITS -- read before trusting this elsewhere. The patterns are
derived from the drop_reason *wording* of one model family (deepseek-v4-*)
on one eval corpus. They are load-bearing regexes, not semantics: a
provider/model change or a prompt rewrite can silently reduce their recall
to zero (they fail toward "no rescue", never toward false keeps, and every
rescue is tagged "[sentinel:tag]" in dissent_reason, so the blast radius
is auditability, not correctness). Whether they generalize to unseen
repos/wording is an OPEN question, scheduled as the W16 real-PR
generalization gate. If you change providers, re-run the sweep before
believing any recall number.

All functions here are pure and unit-testable offline.
"""
import re

_GUARD_RE = re.compile(
    r"\b(duplicate|restat\w*|already\s+(kept|covered|dropped))\b", re.I)

# Dead-path family. The drop dismisses the finding as intentional / future
# work -- either a hypothesized future caller, or "deliberate design /
# scaffolding / not wired yet". Evidence rule 3 forbids BOTH when the issue
# shows the path is disabled by an existing config/flag VALUE (as opposed
# to merely having no callers, which the rule legitimately allows dropping).
# So the issue gate requires a named ALL-CAPS constant set to a boolean (or
# a flag/guard/condition stated true/false), NOT bare "no callers / dead
# code" -- that gate is the discriminator that keeps the reverse-rule
# no-callers drops out. (Bench round 1: live drops of the config-disabled
# dead path varied the wording to "intentional scaffolding, not wired yet",
# which the modal-only reason missed.)
_DEAD_PATH_DISMISS_REASON_RE = re.compile(
    r"callers?\s+(can|could|may|might)\s+(pass|supply|set|provide)\b"
    r"|intentional\s+(design|behavior|choice|future|scaffold)"
    r"|deliberate\s+(behavior|design|choice)"
    r"|not\s+dead\s+code|scaffold"
    r"|(is|are)n.?t\s+(yet\s+)?wired|not\s+(yet\s+)?wired|wired\s+up\s+later"
    r"|work.in.progress", re.I)
_CONFIG_DISABLED_ISSUE_RE = re.compile(
    r"(?-i:[A-Z][A-Z0-9_]{3,})\s*(={1,2}|\bis\b)\s*(true|false)"
    r"|\b(flag|guard|condition|config)\b[^.]{0,40}\b"
    r"(true|false|set|unset|disabled|never\s+(holds|runs|executes|true))",
    re.I)

# Numeric family. A generic/speculative dismissal against a finding that
# CLAIMS an invariant is lost across repeated updates (or a missing term).
# Naming invariant vocabulary as mere context (e.g. "S is positive definite"
# in an inv-vs-solve nit) is not a claim, so the issue must also carry a
# loss verb. (Sweep iteration 1: vocabulary alone false-rescued d10's
# inv-vs-solve control; bench round 1: the reason also appears as
# "speculative robustness / no concrete defect".)
_GENERIC_DISMISS_REASON_RE = re.compile(
    r"(generic|speculative)\s+(numerical\s+)?(best.?practice|robustness)"
    r"|textbook\b|no\s+concrete\s+(failure|drift|defect)", re.I)
_INVARIANT_VOCAB_RE = re.compile(
    r"invariant|symmetr|positive.?(semi.?)?definite|conservation", re.I)
_INVARIANT_LOSS_RE = re.compile(
    r"\blo(?:se|ses|sing|st)\b|violat|no\s+longer|drift", re.I)
_MISSING_TERM_RE = re.compile(r"missing\s+term", re.I)

# Documented-condition family (W15). Four recorded kills (w10r3-d6,
# w11r3-d5, W14 slice r1-d6, W14 full r3-d5) dismiss as "tuning /
# speculative / not a concrete defect" a finding whose issue CITES an
# existing doc assertion that the input condition occurs -- reasoning
# evidence rule 1 forbids ("documenting a condition is not handling it";
# "no evidence the scenario occurs" is refuted by the citation itself,
# and "correctly returns None" is the locally-correct-but-functionally-
# dead shape that killed d5 back in W5). The issue gate demands a
# POSITIVE citation (comment/docstring + notes/states/says/documents, a
# "documents that", or a quoted comment); missing-doc nits ("docstring
# does not specify X") carry no such assertion verb and stay dropped --
# that keeps the 31 legitimately dismissal-phrased W14 drops out
# (sweep-validated against W11/W12/W14 full + W14 slice).
_DOC_DISMISS_REASON_RE = re.compile(
    r"(not\s+a|rather\s+than\s+a)\s+(concrete\s+)?(defect|bug)"
    r"|(parameter|threshold).?tuning\s+(suggestion|observation)"
    r"|no\s+evidence\s+that"
    r"|speculative\s+(robustness|future.?proofing)"
    r"|(handles?|works?)\b[^.]{0,50}\b(gracefully|correctly)"
    r"|correctly\s+returns?", re.I)
# Sweep iteration 2: "does not document that X" is the missing-doc nit in
# citation clothing -- a negated verb is not a citation.
_DOC_CITED_ISSUE_RE = re.compile(
    r"\b(comment|caller|constant|docstring)\b.{0,60}?"
    r"(?<!not )(?<!n't )\b(notes?|states?|says?|documents?)\s+that\b"
    r"|with\s+(the\s+)?comment\s+[\"'“]", re.I)


def classify_drop(f: dict) -> str | None:
    """Sentinel verdict for one dropped finding: a rescue tag, the string
    "duplicate-guard" (pattern hit but the drop is a duplicate call -- never
    rescue those, one bug must not re-inflate into two findings), or None.
    Pure function, unit-testable offline."""
    reason = f.get("drop_reason", "")
    issue = f.get("issue", "")
    tag = None
    claims_invariant = ((_INVARIANT_VOCAB_RE.search(issue)
                         and _INVARIANT_LOSS_RE.search(issue))
                        or _MISSING_TERM_RE.search(issue))
    if (_DEAD_PATH_DISMISS_REASON_RE.search(reason)
            and _CONFIG_DISABLED_ISSUE_RE.search(issue)):
        tag = "dead-path-dismissed"
    elif _GENERIC_DISMISS_REASON_RE.search(reason) and claims_invariant:
        tag = "numeric-invariant"
    elif (_DOC_DISMISS_REASON_RE.search(reason)
            and _DOC_CITED_ISSUE_RE.search(issue)):
        tag = "doc-condition-dismissed"
    if tag and _GUARD_RE.search(reason):
        return "duplicate-guard"
    return tag


def rescue_forbidden_drops(dropped: list) -> tuple[list, list]:
    """(rescued_as_uncertain, still_dropped). Rescued findings lose the
    drop_reason and enter the uncertain channel; the fixed "[sentinel:tag] "
    prefix keeps the original reason machine-recoverable. Pure function."""
    rescued, still = [], []
    for f in dropped:
        tag = classify_drop(f)
        if tag and tag != "duplicate-guard":
            r = {k: v for k, v in f.items() if k != "drop_reason"}
            r["verification"] = "uncertain"
            r["dissent_reason"] = f"[sentinel:{tag}] {f.get('drop_reason', '')}"
            r["rescue"] = tag
            rescued.append(r)
        else:
            still.append(f)
    return rescued, still
