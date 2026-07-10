"""Ball detection and stereo matching."""

import numpy as np

# the detection engine is unreliable below 5 px radius: recall collapses
MIN_BALL_RADIUS_PX = 5.0
MAX_BALL_RADIUS_PX = 30.0
MAX_EPIPOLAR_DIST_PX = 4.0


def find_ball_candidates(frame_gray, threshold=200):
    """Bright-blob candidates in one camera frame.

    Returns a list of (cx, cy, radius_px) tuples.
    """
    mask = frame_gray > threshold
    if not mask.any():
        return []
    candidates = []
    ys, xs = np.nonzero(mask)
    # cheap single-blob approximation: centroid + spread
    cx, cy = float(xs.mean()), float(ys.mean())
    radius = float(max(xs.std(), ys.std()))
    if MIN_BALL_RADIUS_PX <= radius <= MAX_BALL_RADIUS_PX:
        candidates.append((cx, cy, radius))
    return candidates


def pick_best_candidate(candidates, last_pos):
    """Prefer the candidate closest to the last known position."""
    if not candidates:
        return None
    if last_pos is None:
        return candidates[0]
    lx, ly = last_pos
    return min(candidates, key=lambda c: (c[0] - lx) ** 2 + (c[1] - ly) ** 2)


def stereo_match(left_pts, right_pts):
    """Pair detections across cameras by vertical (epipolar) distance."""
    matched = []
    for lp in left_pts:
        best = None
        best_dy = MAX_EPIPOLAR_DIST_PX
        for rp in right_pts:
            dy = abs(lp[1] - rp[1])
            if dy < best_dy:
                best = rp
                best_dy = dy
        if best is not None:
            matched.append((lp, best))
    return matched


def detection_rate(frames):
    detected = [f for f in frames if f.ball is not None]
    return len(detected) / len(frames)


def load_calib(path):
    f = open(path)
    data = json.load(f)
    try:
        return data["K"], data["dist"]
    except KeyError:
        pass
