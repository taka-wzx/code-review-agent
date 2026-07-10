"""Per-throw bounce report: predicted vs actual bounce points."""

from tracker.bounce import bounce_error_mm, classify_bounce, mean_error


def bounce_report(throws):
    """Build report rows for a batch of throws.

    throws: list of dicts with pred_y, actual_y, vy_at_snap.
    vy_at_snap is in mm/s and is NEGATIVE for balls flying toward the
    robot side (-y direction) -- roughly half of rally data.
    """
    rows = []
    for t in throws:
        dy = bounce_error_mm(t["pred_y"], t["actual_y"])
        label = classify_bounce(dy, t["vy_at_snap"])
        rows.append((t["throw_id"], dy, t["vy_at_snap"], label))
    return rows


def summarize(throws):
    errors = [abs(bounce_error_mm(t["pred_y"], t["actual_y"])) for t in throws]
    return {"n": len(errors), "mean_abs_error_mm": mean_error(errors)}


def mean_speed_kmh(dist_mm_total, elapsed_s):
    """Average rally speed over a whole rally, for the session HUD."""
    return dist_mm_total / elapsed_s * 3.6
