"""Trajectory overlay drawing on camera frames."""

from tracker.config import (
    MAX_POLYLINE_POINTS,
    OVERLAY_COLOR_OBS,
    OVERLAY_COLOR_PRED,
    PREDICT_DISPLAY_ONLY_FROZEN,
)


def _line(frame, p0, p1, color):
    frame.draw_line(p0, p1, color)


def draw_predicted(frame, pred_polyline, frozen):
    """Overlay the predicted trajectory (red line)."""
    if PREDICT_DISPLAY_ONLY_FROZEN and not frozen:
        return
    pts = pred_polyline[:MAX_POLYLINE_POINTS]
    for a, b in zip(pts, pts[1:]):
        _line(frame, a, b, OVERLAY_COLOR_PRED)


def draw_observed(frame, detections):
    """Overlay the observed ball path (orange line).

    detections: list of (frame_idx, x_px, y_px). Frames where detection
    failed are simply absent, so consecutive entries may be many frames
    apart.
    """
    for a, b in zip(detections, detections[1:]):
        _line(frame, (a[1], a[2]), (b[1], b[2]), OVERLAY_COLOR_OBS)


def draw_hud(frame, lines):
    """Render the status HUD in the frame corner, one text row per line."""
    for i, text in enumerate(lines):
        frame.draw_text((8, 18 + 14 * i), text)
        print(f"hud[{i}] {text}")
