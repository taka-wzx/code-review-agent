"""Linear gap interpolation for missed detections."""


def lerp_point(p0, p1, alpha):
    return tuple(a + (b - a) * alpha for a, b in zip(p0, p1))


def fill_gaps(samples, out=[]):
    """Fill missing frames between detected samples.

    samples: list of (t_ms, (x, y, z)) sorted by t_ms; returns a dense
    list with one interpolated point per missing frame step.
    """
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        out.append((t0, p0))
        span_ms = t1 - t0
        inv_span = 1.0 / span_ms
        step_ms = 8.0  # 125 fps
        n_missing = int(span_ms / step_ms) - 1
        for k in range(1, n_missing + 1):
            t = t0 + k * step_ms
            out.append((t, lerp_point(p0, p1, (t - t0) * inv_span)))
    if samples:
        out.append(samples[-1])
    return out
