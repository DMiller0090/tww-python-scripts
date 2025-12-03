# tuner_phase_profiler.py
# Build a phase ledger from a per-frame array, overwriting the phase on each frame.
# Savestate rewinds naturally work because we always rebuild the ledger from 0..now.

from dolphin import event, gui
from typing import Optional, List, Tuple
from ww import game, actor as actor_mod
from ww.actors.gba import GBA

# ── Display knobs ─────────────────────────────────────────────────────────────
OVERLAY_XY = (15, 250)
COLOR      = 0xFFFFFFFF  # ARGB white
SUMMARY_HOLD_FRAMES = 240  # how long to keep last completed summary visible

# ── State ─────────────────────────────────────────────────────────────────────
_gate = game.FrameGate()

_gba: Optional[GBA] = None

# phases_by_frame[f] = upload_action phase for that frame (int).
PHASE_UNSET = -1
phases_by_frame: List[int] = []

# Keep last completed summary so we can display it for a while
_last_summary_lines: List[str] = []
_last_summary_expire_at: Optional[int] = None


def _find_gba() -> Optional[GBA]:
    for a in actor_mod.iter_actors("PROC_AGB"):
        if isinstance(a, GBA):
            return a
        try:
            return GBA(a.base)
        except Exception:
            return None
    return None


def _ensure_len(n: int) -> None:
    if n > len(phases_by_frame):
        phases_by_frame.extend([PHASE_UNSET] * (n - len(phases_by_frame)))


def _read_phase() -> Optional[int]:
    """Read GBA upload_action; return None if unreadable."""
    global _gba
    if _gba is None:
        _gba = _find_gba()
        if _gba is None:
            return None
    try:
        ph = _gba.upload_action(default=-1)
        return ph if ph >= 0 else None
    except Exception:
        return None


def _build_ledger(upto_frame: int) -> Tuple[bool, List[Tuple[int, int]], Optional[int], Optional[int], int]:
    """
    Walk phases_by_frame from 0..upto_frame and produce:
      - completed: bool (session closed by returning to 0)
      - lines: list[(phase, duration)] in order encountered (completed segments)
      - live_phase: Optional[int] (current ongoing phase if session active)
      - live_elapsed: Optional[int] (elapsed duration for the ongoing phase)
      - total: int total duration in current session so far (or final total if completed)
    A "session" starts when we first see phase != 0 and ends when we see phase == 0.
    """
    lines: List[Tuple[int, int]] = []
    completed = False
    total = 0

    session_active = False
    live_phase = None            # type: Optional[int]
    live_elapsed = None          # type: Optional[int]

    cur_phase = None             # type: Optional[int]
    cur_len = 0

    def close_block():
        nonlocal cur_phase, cur_len, lines, total
        if cur_phase is None:
            return
        if session_active:
            lines.append((cur_phase, cur_len))
            total += cur_len
        cur_phase, cur_len = None, 0

    i = 0
    while i <= upto_frame:
        p = phases_by_frame[i] if i < len(phases_by_frame) else PHASE_UNSET

        if p == PHASE_UNSET:
            i += 1
            continue

        if not session_active:
            if p != 0:
                session_active = True
                cur_phase, cur_len = p, 1
            # else still idle at zero
        else:
            if p == 0:
                close_block()
                completed = True
                break  # freeze the first complete session encountered
            else:
                if cur_phase is None:
                    cur_phase, cur_len = p, 1
                elif p == cur_phase:
                    cur_len += 1
                else:
                    close_block()
                    cur_phase, cur_len = p, 1

        i += 1

    if session_active and not completed:
        live_phase = cur_phase
        live_elapsed = cur_len

    return completed, lines, live_phase, live_elapsed, total


def _freeze_summary(lines: List[Tuple[int, int]], total: int, now_frame: int) -> None:
    global _last_summary_lines, _last_summary_expire_at
    out = ["Tingle Tuner / upload_action", "— Session Complete —"]
    for ph, dur in lines:
        out.append(f"Phase {ph}: {dur}")
    out.append(f"Total: {total}")
    _last_summary_lines = out
    _last_summary_expire_at = now_frame + SUMMARY_HOLD_FRAMES


def _draw(lines: List[str]) -> None:
    if lines:
        gui.draw_text(OVERLAY_XY, COLOR, "\n".join(lines))


@event.on_frameadvance
def update():
    if not _gate.gate():
        return

    f = game.frame()

    # Expire prior summary if needed (we just stop drawing after expiry)
    if (_last_summary_expire_at is not None) and (f > _last_summary_expire_at):
        pass

    # Read current phase and ALWAYS overwrite this frame’s entry
    phase = _read_phase()
    if phase is not None:
        _ensure_len(f + 1)
        phases_by_frame[f] = phase  # <— overwrite on every frame

    # Build ledger up to now (future frames after a rewind are ignored)
    completed, lines, live_phase, live_elapsed, subtotal = _build_ledger(f)

    if completed:
        total = sum(d for _, d in lines)
        _freeze_summary(lines, total, f)
        _draw(_last_summary_lines)
        return

    if lines or (live_phase is not None):
        hud = ["Tingle Tuner: in progress"]
        for ph, dur in lines:
            hud.append(f"Phase {ph}: {dur}")
        if live_phase is not None and live_elapsed is not None:
            hud.append(f"Phase {live_phase}: {live_elapsed}")
            hud.append(f"Total: {subtotal + live_elapsed}")
        else:
            hud.append(f"Total: {subtotal}")
        _draw(hud)
        return

    # No session yet → show last summary (if still within hold), else waiting
    if (_last_summary_expire_at is not None) and (f <= _last_summary_expire_at):
        _draw(_last_summary_lines)
    else:
        _draw(["Tingle Tuner: waiting (phase 0)"])
