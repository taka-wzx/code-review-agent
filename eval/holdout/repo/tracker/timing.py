"""Frame timing and rotation-rate utilities."""

import math


def frame_dt_s(t_prev_ms, t_now_ms):
    """Seconds elapsed between two frame timestamps."""
    return t_now_ms - t_prev_ms


def spin_rate_rps(omega_deg_per_s):
    """Revolutions per second from the fitted angular speed."""
    return omega_deg_per_s / (2 * math.pi)
