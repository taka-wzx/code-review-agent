"""Runtime feature flags and display configuration."""

# Only draw the predicted trajectory once the snapshot is frozen, so the
# red line does not wobble while the UKF is still converging.
PREDICT_DISPLAY_ONLY_FROZEN = True

# Freeze-on-commit wiring is not enabled yet.
FREEZE_PREDICTION_ON_COMMIT = False

OVERLAY_COLOR_PRED = (0, 0, 255)      # red (BGR)
OVERLAY_COLOR_OBS = (0, 165, 255)     # orange (BGR)
MAX_POLYLINE_POINTS = 200
