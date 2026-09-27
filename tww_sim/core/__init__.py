"""tww_sim.core — the shared player engine (component-neutral, pure stdlib).

  ``fp``       FMA-faithful f32 primitives (fmadds/fmsubs/…) — the Gekko FP kernel.
  ``mathlib``  console trig + tables (cM_scos/cM_ssin), cLib_addCalc, J3DFrameCtrl, stick helpers.
  ``camera``   the C-stick yaw camera model (sub-package).
  ``anim``     the J3D animation runtime (sub-package): BCK eval, quaternions, world-space FK.
  ``tables/``  baked console lookup data (cos/sin/omega/stick grids).
  (``collision`` — future: c_m3d / dBgS seam-clip predicate; see the design doc §6.)

Common primitives are re-exported here; ``camera`` and ``anim`` are imported explicitly as
sub-packages (NOT eager-loaded) so ``import tww_sim.core`` stays cheap.
"""
from .fp import f32  # noqa: F401
from .mathlib import (  # noqa: F401
    nfmod, fc_update, cLib_addCalc, cM_scos, cM_ssin_s16, cM_scos_s16,
    deg_to_s16, s16_signed, _F32_PI, ARROW_STICK_DEADZONE, angdiff_deg,
    _deadzone, stick_angle_deg, _COS_TABLE, _SIN_TABLE,
    clamp_stick, main_stick_decode,
)
