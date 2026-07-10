"""Runtime feature flags and display configuration."""

# --- prediction display ------------------------------------------------------

# Only draw the predicted trajectory once the snapshot is frozen, so the
# red line does not wobble while the UKF is still converging.
PREDICT_DISPLAY_ONLY_FROZEN = True

# Freeze-on-commit wiring is not enabled yet.
FREEZE_PREDICTION_ON_COMMIT = False

# --- overlay colours (BGR) ---------------------------------------------------

OVERLAY_COLOR_PRED = (0, 0, 255)      # red: predicted trajectory
OVERLAY_COLOR_OBS = (0, 165, 255)     # orange: observed path

# --- overlay limits ----------------------------------------------------------

MAX_POLYLINE_POINTS = 200
