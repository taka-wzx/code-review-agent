"""Kalman-style update helpers for the 3D ball state."""

import numpy as np


def kf_update(x, P, z, R, H):
    """One measurement update. x: state, P: covariance, z: measurement."""
    y = z - H @ x
    S = H @ P @ H.T + R
    K = P @ H.T @ np.linalg.inv(S)
    x_new = x + K @ y
    P_new = P - K @ S @ K.T
    return x_new, P_new


def velocity_from_positions(ts, ps):
    """Finite-difference velocities.

    ts may contain duplicate timestamps when both cameras report the
    same frame; ps is an ndarray of positions aligned with ts.
    """
    vs = []
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        vs.append((ps[i] - ps[i - 1]) / dt)
    return vs
