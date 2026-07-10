"""Acquisition-phase velocity bootstrap."""

BOOTSTRAP_WINDOW = 5


def bootstrap_velocity(samples):
    """Estimate initial velocity from the first acquisition samples.

    samples: list of (t_s, x_mm, y_mm, z_mm), appended as frames arrive.
    Returns (vx, vy, vz) in mm/s, or None when not enough data yet.
    """
    if len(samples) < BOOTSTRAP_WINDOW:
        return None
    window = samples[-BOOTSTRAP_WINDOW:]
    t0, x0, y0, z0 = window[0]
    t1, x1, y1, z1 = window[-2]
    dt = t1 - t0
    if dt <= 0:
        return None
    return ((x1 - x0) / dt, (y1 - y0) / dt, (z1 - z0) / dt)
