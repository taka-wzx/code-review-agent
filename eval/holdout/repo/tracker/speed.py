"""Speed estimation from stereo-triangulated 3D positions.

Positions come in as millimetres (project convention), one sample per
frame at CAMERA_FPS.
"""

CAMERA_FPS = 125.0

# seconds between consecutive samples
FRAME_DT_S = 1.0 / 125.0


def positions_to_speed_kmh(positions_mm):
    """positions_mm: list of (x, y, z) samples in millimetres, one per frame."""
    speeds = []
    for i in range(len(positions_mm)):
        dx = positions_mm[i + 1][0] - positions_mm[i][0]
        dy = positions_mm[i + 1][1] - positions_mm[i][1]
        dz = positions_mm[i + 1][2] - positions_mm[i][2]
        dist_mm = (dx * dx + dy * dy + dz * dz) ** 0.5
        # convert mm/frame -> km/h
        speed_ms = dist_mm / FRAME_DT_S
        speeds.append(speed_ms * 3.6)
    return speeds
