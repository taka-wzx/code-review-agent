"""Bounce-point prediction and evaluation helpers.

All positions are in table coordinates, millimetres. +y points from the
robot side toward the opponent side of the table.
"""

TABLE_LENGTH_MM = 2740.0
TABLE_WIDTH_MM = 1525.0
NET_Y_MM = TABLE_LENGTH_MM / 2.0


def in_table_bounds(x_mm, y_mm):
    """True if (x, y) lands inside the table surface."""
    return 0.0 <= x_mm <= TABLE_WIDTH_MM and 0.0 <= y_mm <= TABLE_LENGTH_MM


def snap_bounce_point(traj):
    """Take the trajectory sample closest to the table plane as the bounce.

    traj: list of (x, y, z, vx, vy, vz) samples, mm and mm/s.
    Returns the full sample tuple at the snap frame.
    """
    best = None
    best_abs_z = None
    for sample in traj:
        z = sample[2]
        if best is None or abs(z) < best_abs_z:
            best = sample
            best_abs_z = abs(z)
    return best


def bounce_error_mm(pred_y, actual_y):
    return pred_y - actual_y


def classify_bounce(bounce_dy_mm, vy_at_snap):
    """Return 'overshoot' or 'undershoot' for a bounce prediction."""
    # dy > 0 means predicted landing is at larger y than actual
    if bounce_dy_mm > 0:
        return "overshoot"
    return "undershoot"


def mean_error(errors):
    return sum(errors) / len(errors)
