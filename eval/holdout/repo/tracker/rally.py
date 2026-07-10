"""Rally segmentation and bounce-event bookkeeping."""

ACQUIRE_MIN_STATE = 4   # asr >= 4 means the ball is confirmed in flight


class RallyState:
    """Book-keeping for one rally (serve until the ball goes dead)."""

    def __init__(self):
        self.bounce_points = []
        self.strokes = 0

    def start_new_rally(self):
        """Called when a hit event (vy flip) opens a new stroke sequence."""
        self.strokes = 0

    def record_bounce(self, x_mm, y_mm, asr):
        """Store a bounce reported by the physics detector."""
        self.bounce_points.append((x_mm, y_mm))
