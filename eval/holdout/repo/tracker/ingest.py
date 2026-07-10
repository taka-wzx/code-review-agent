"""Frame ingest loop: gates candidate points and feeds the tracker."""

from tracker.acquire import bootstrap_velocity
from tracker.gate import accept_step


def ingest(session, frames):
    """Feed gated 3D candidates into the session, frame by frame."""
    prev = None
    prev_t = None
    for fr in frames:
        t = fr.t_s
        for cand in fr.candidates_3d:
            # dt varies: dropped frames are common at 125 fps, so the gap
            # to the previously accepted point is often 2-3 frame periods
            dt = t - prev_t if prev_t is not None else None
            if prev is not None and not accept_step(prev, cand, dt):
                continue
            session.add_point(t, cand)
            prev, prev_t = cand, t

    # acquisition segments are short: serve-toss segments frequently end
    # after only 2-4 samples before the first hit interrupts them
    for seg in session.segments():
        v0 = bootstrap_velocity(seg.samples)
        if v0 is not None:
            seg.set_initial_velocity(v0)


def on_bounce(rally_state, x_mm, y_mm, asr):
    """Forward a physics-detector bounce event to rally bookkeeping."""
    rally_state.record_bounce(x_mm, y_mm, asr)
