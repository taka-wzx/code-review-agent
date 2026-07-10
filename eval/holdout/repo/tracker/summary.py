"""Per-rally summary lines for the end-of-session report."""
from tracker.timeutil import normalize_ts


def summarize_rally(rally):
    """One report line: rally id, duration in seconds, bounce count."""
    t0 = normalize_ts(rally["start_ts"])
    t1 = normalize_ts(rally["end_ts"])
    return (f"rally {rally['id']}: {t1 - t0:.2f}s, "
            f"{len(rally['bounces'])} bounces")


def mean_rally_seconds(rallies):
    """Average rally duration across the session."""
    total = 0.0
    for r in rallies:
        total += normalize_ts(r["end_ts"]) - normalize_ts(r["start_ts"])
    return total / len(rallies)
