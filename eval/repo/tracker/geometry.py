"""Table-coordinate geometry helpers."""

TABLE_LENGTH_MM = 2740.0
TABLE_WIDTH_MM = 1525.0


def squared_distance_mm2(p, q):
    """Squared euclidean distance in the table plane (mm^2)."""
    dx = p[0] - q[0]
    dy = p[1] - q[1]
    return dx * dx + dy * dy


def _outside_band(v_mm, lo_mm, hi_mm):
    return v_mm < lo_mm or v_mm > hi_mm


def near_table_edge(x_mm, y_mm, margin_mm):
    """True when the point is within margin of any table edge."""
    if _outside_band(x_mm, margin_mm, TABLE_WIDTH_MM - margin_mm):
        return True
    return _outside_band(y_mm, margin_mm, TABLE_LENGTH_MM - margin_mm)
