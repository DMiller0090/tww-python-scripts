#!/usr/bin/env python3
"""tww_sim.core.mathlib — the console math primitives shared by every component.

Gekko/Broadway (PPC 750CL) faithful f32 trig + helpers, split out of the old swim
``sim.py`` so land, swim, anim, and the future collision core can import ONLY these
(no swim-physics dependency). ``f32`` and the fused ops live in :mod:`tww_sim.core.fp`;
this module owns the console cosine/sine tables, ``cM_scos``/``cM_ssin``, ``cLib_addCalc``,
the ``J3DFrameCtrl`` loop (``fc_update``), the s16 angle helpers, and the stick dead-zone /
angle primitives. All bit-exact vs Dolphin (see knowledge/model/fp-faithfulness.md).
"""
import math, struct
import os as _os

from .fp import f32, fdivs, fmuls

def nfmod(a, n):
    return a - math.floor(a / n) * n

def fc_update(frame, rate, end, start=0.0, loop=0.0):
    """Faithful J3DFrameCtrl::update() LOOP mode (J3DAnimation.cpp:143-186): advance by
    mRate, then loop by REPEATED f32 subtraction of (mEnd - mLoop) -- NOT a single modulo.
    For frames already in [start, end+rate) this is one subtraction == nfmod (so the no-pump
    baselines stay bit-exact). It ONLY differs after the x598 pump scramble, where mFrame
    is ~15232 and the game subtracts (end-loop) ~662 times in f32: that accumulated f32
    rounding is the ~0.004 entry residual that compounded across pump cycles under nfmod.
    SWIMING/SWIMWAIT both use mStart=0, mLoop=0 -> the loop subtracts `end` each step."""
    f = f32(frame + rate)
    span_lo = loop - start
    while f < start and span_lo > 0.0:
        f = f32(f + span_lo)
    span_hi = end - loop
    if span_hi <= 0.0:
        return f
    while f >= end:
        f = f32(f - span_hi)
    return f

def cLib_addCalc(value, target, scale, max_step, min_step):
    """Faithful cLib_addCalc (c_lib.cpp): chase `value` toward `target`. step = scale*(target
    -value); if |step|>=min_step clamp to +-max_step and apply; else snap by +-min_step
    (clamped so it doesn't overshoot target). All f32. Used for the neutral speed decay."""
    if value == target:
        return value
    step = f32(scale * f32(target - value))
    if step >= min_step or step <= -min_step:
        if step > max_step:
            step = max_step
        if step < -max_step:
            step = -max_step
        return f32(value + step)
    if step > 0.0:
        if step < min_step:
            nv = f32(value + min_step)
            return target if nv > target else nv
    else:
        ms = -min_step
        if step > ms:
            nv = f32(value + ms)
            return target if nv < target else nv
    return value

# cM_scos indexes the ACTUAL console jmaCosTable (dumped live @ 0x80498168), not f32(cos(x)):
# see knowledge/model/fp-faithfulness.md and history/resolved-bugs.md for why the x86 recompute desynced.
with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'tables', 'cos_table.bin'), 'rb') as _f:
    _COS_TABLE = struct.unpack('>4096f', _f.read())   # console-libm values, big-endian f32

# The SIN companion for the quaternion foot-FK: the REAL console jmaSinTable (dumped live @ 0x80497168),
# NOT a -1024 view of cos -- those differ 1 ULP at 816/4096 entries. See knowledge/model/anim-engine.md.
with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'tables', 'sin_table.bin'), 'rb') as _f:
    _SIN_TABLE = struct.unpack('>4096f', _f.read())   # console-libm jmaSinTable[0:4096], big-endian f32

def cM_ssin_s16(angle):
    """JMASSin: jmaSinTable[(u16)angle >> 4] -- exact console value from the baked sin table."""
    return _SIN_TABLE[(int(angle) & 0xFFFF) >> 4]

_RAD2IDX = 10430.3779296875                 # 65536 / 2pi (cM_rad2s scale)
_GAME_TWOPI = 6.283185482025146             # the f32 2pi the game wraps with
def cM_scos(rad):
    value = rad % _GAME_TWOPI
    index = int(value * _RAD2IDX)
    if index < -32768:
        index += 65536
    elif index > 32767:
        index -= 65536
    index >>= 4                              # 65536 angles -> 4096 entries, low bits dropped
    if index < 0:
        index = 4096 + index
    return _COS_TABLE[index]                  # exact console table value (was f32(cos(...)))

def cM_scos_s16(angle):
    """The game's cM_scos applied DIRECTLY to an s16 angle (no cM_rad2s). This is what
    setSpeedAndAngleSwim uses: cM_scos(shape_angle.y - oldAngleY) where the arg is already
    s16. JMASCos: jmaCosTable[(u16)angle >> 4] -- exact console value from the baked table."""
    index = (int(angle) & 0xFFFF) >> 4          # 65536 angles -> 4096 entries, low bits drop
    return _COS_TABLE[index]

def deg_to_s16(deg):
    return int(round(deg / 360.0 * 65536.0)) & 0xFFFF

def s16_signed(a):
    a &= 0xFFFF
    return a - 65536 if a >= 32768 else a

# The console atan table (c_math.cpp atntable[1025]), embedded VERBATIM from the decomp --
# do not regenerate from atan(): the original tool's rounding differs at edges.
_ATN_HEX = (
    '0000000a0014001f00290033003d00470051005c00660070007a0084008f009900a300ad00b700c200cc00d600e000ea'
    '00f400ff01090113011d01270131013c01460150015a0164016f01790183018d019701a101ac01b601c001ca01d401de'
    '01e901f301fd02070211021b02260230023a0244024e02580262026d02770281028b0295029f02a902b402be02c802d2'
    '02dc02e602f002fb0305030f03190323032d03370341034c03560360036a0374037e03880392039c03a703b103bb03c5'
    '03cf03d903e303ed03f70401040c04160420042a0434043e04480452045c04660470047a0484048e049904a304ad04b7'
    '04c104cb04d504df04e904f304fd05070511051b0525052f05390543054d05570561056b0575057f05890593059d05a7'
    '05b105bb05c505cf05d905e305ed05f70601060b0615061f06290633063d06470651065b0665066e06780682068c0696'
    '06a006aa06b406be06c806d206dc06e506ef06f90703070d07170721072b0735073e07480752075c07660770077a0783'
    '078d079707a107ab07b507be07c807d207dc07e607ef07f90803080d08170820082a0834083e08480851085b0865086f'
    '08780882088c0896089f08a908b308bd08c608d008da08e308ed08f70901090a0914091e09270931093b0944094e0958'
    '0961096b0975097e09880992099b09a509ae09b809c209cb09d509de09e809f209fb0a050a0e0a180a220a2b0a350a3e'
    '0a480a510a5b0a640a6e0a770a810a8b0a940a9e0aa70ab10aba0ac40acd0ad70ae00ae90af30afc0b060b0f0b190b22'
    '0b2c0b350b3f0b480b510b5b0b640b6e0b770b800b8a0b930b9d0ba60baf0bb90bc20bcb0bd50bde0be70bf10bfa0c03'
    '0c0d0c160c1f0c290c320c3b0c450c4e0c570c600c6a0c730c7c0c860c8f0c980ca10cab0cb40cbd0cc60ccf0cd90ce2'
    '0ceb0cf40cfd0d070d100d190d220d2b0d340d3e0d470d500d590d620d6b0d740d7d0d870d900d990da20dab0db40dbd'
    '0dc60dcf0dd80de10dea0df30dfc0e050e0f0e180e210e2a0e330e3c0e450e4e0e560e5f0e680e710e7a0e830e8c0e95'
    '0e9e0ea70eb00eb90ec20ecb0ed40edc0ee50eee0ef70f000f090f120f1b0f230f2c0f350f3e0f470f500f580f610f6a'
    '0f730f7c0f840f8d0f960f9f0fa70fb00fb90fc20fca0fd30fdc0fe50fed0ff60fff1007101010191021102a1033103b'
    '1044104d1055105e1067106f1078108010891092109a10a310ab10b410bc10c510ce10d610df10e710f010f811011109'
    '1112111a1123112b1134113c1145114d1156115e1166116f1177118011881191119911a111aa11b211bb11c311cb11d4'
    '11dc11e411ed11f511fd1206120e1216121f1227122f12371240124812501259126112691271127a1282128a1292129a'
    '12a312ab12b312bb12c312cc12d412dc12e412ec12f412fc1305130d1315131d1325132d1335133d1345134d1355135e'
    '1366136e1376137e1386138e1396139e13a613ae13b613be13c613ce13d613de13e613ed13f513fd1405140d1415141d'
    '1425142d1435143d1444144c1454145c1464146c1473147b1483148b1493149b14a214aa14b214ba14c114c914d114d9'
    '14e014e814f014f814ff1507150f1516151e1526152d1535153d1544154c1554155b1563156b1572157a158115891591'
    '159815a015a715af15b715be15c615cd15d515dc15e415eb15f315fa160216091611161816201627162f1636163e1645'
    '164c1654165b1663166a1671167916801688168f1696169e16a516ac16b416bb16c216ca16d116d816e016e716ee16f6'
    '16fd1704170b1713171a1721172817301737173e1745174c1754175b1762176917701778177f1786178d1794179b17a2'
    '17aa17b117b817bf17c617cd17d417db17e217e917f017f717fe1806180d1814181b1822182918301837183e1845184c'
    '1853185a18601867186e1875187c1883188a18911898189f18a618ad18b318ba18c118c818cf18d618dd18e318ea18f1'
    '18f818ff1906190c1913191a19211928192e1935193c1943194919501957195d1964196b19721978197f1986198c1993'
    '199a19a019a719ae19b419bb19c219c819cf19d519dc19e319e919f019f619fd1a041a0a1a111a171a1e1a241a2b1a31'
    '1a381a3e1a451a4b1a521a581a5f1a651a6c1a721a791a7f1a861a8c1a931a991a9f1aa61aac1ab31ab91ac01ac61acc'
    '1ad31ad91adf1ae61aec1af21af91aff1b051b0c1b121b181b1f1b251b2b1b321b381b3e1b441b4b1b511b571b5d1b64'
    '1b6a1b701b761b7d1b831b891b8f1b951b9c1ba21ba81bae1bb41bba1bc11bc71bcd1bd31bd91bdf1be51beb1bf21bf8'
    '1bfe1c041c0a1c101c161c1c1c221c281c2e1c341c3a1c401c461c4c1c521c581c5e1c641c6a1c701c761c7c1c821c88'
    '1c8e1c941c9a1ca01ca61cac1cb21cb81cbe1cc31cc91ccf1cd51cdb1ce11ce71ced1cf31cf81cfe1d041d0a1d101d16'
    '1d1b1d211d271d2d1d331d381d3e1d441d4a1d4f1d551d5b1d611d661d6c1d721d781d7d1d831d891d8e1d941d9a1da0'
    '1da51dab1db11db61dbc1dc21dc71dcd1dd31dd81dde1de31de91def1df41dfa1dff1e051e0b1e101e161e1b1e211e26'
    '1e2c1e321e371e3d1e421e481e4d1e531e581e5e1e631e691e6e1e741e791e7f1e841e8a1e8f1e941e9a1e9f1ea51eaa'
    '1eb01eb51eba1ec01ec51ecb1ed01ed51edb1ee01ee61eeb1ef01ef61efb1f001f061f0b1f101f161f1b1f201f261f2b'
    '1f301f361f3b1f401f451f4b1f501f551f5a1f601f651f6a1f6f1f751f7a1f7f1f841f8a1f8f1f941f991f9e1fa41fa9'
    '1fae1fb31fb81fbd1fc31fc81fcd1fd21fd71fdc1fe11fe61fec1ff11ff61ffb2000'
)
_ATN_TABLE = tuple(int(_ATN_HEX[i:i + 4], 16) for i in range(0, len(_ATN_HEX), 4))
assert len(_ATN_TABLE) == 1025

# cM3d_IsZero's threshold (c_m3d.cpp:22 G_CM3D_F_ABS_MIN = 3.8146973e-06f == 2^-18).
G_CM3D_F_ABS_MIN = 2.0 ** -18


def cM_atan2s(f0, f1):
    """cM_atan2s (c_math.cpp:118): table atan2 -> u16 angle. U_GetAtanTable's index is the
    single-precision (int)(f0/f1 * 1024) with fdivs+fmuls, truncated."""
    f0 = f32(f0); f1 = f32(f1)
    if abs(f0) < G_CM3D_F_ABS_MIN:
        return 0 if f1 >= 0.0 else 0x8000
    if abs(f1) < G_CM3D_F_ABS_MIN:
        return 0x4000 if f0 >= 0.0 else 0xC000
    def tab(a, b):
        return _ATN_TABLE[int(fmuls(fdivs(a, b), 1024.0))]
    if f0 >= 0.0:
        if f1 >= 0.0:
            r = tab(f0, f1) if f1 >= f0 else 0x4000 - tab(f1, f0)
        else:
            r = (tab(-f1, f0) + 0x4000) if -f1 < f0 else 0x8000 - tab(f0, -f1)
    elif f1 < 0.0:
        r = (tab(-f0, -f1) + 0x8000) if f1 <= f0 else 0xC000 - tab(-f1, -f0)
    else:
        r = (tab(f1, -f0) + 0xC000) if f1 < -f0 else -tab(-f0, f1)
    return r & 0xFFFF

# Console loads M_PI SINGLE (lfs) then fmuls -> cos args use f32 pi, not double math.pi; the
# 1-ULP diff flips cM_rad2s's truncated cell at knife-edges. Rationale: knowledge/model/fp-faithfulness.md.
_F32_PI = f32(math.pi)          # 3.1415927410125732 -- what `lfs M_PI` loads

# Per-axis stick dead-zone: each axis is offset by 15 before the angle/magnitude are taken
# (same 15 as ess_decay). Shared by swim (arrow/ESS) and land (walk stick). Slot-9 validated.
ARROW_STICK_DEADZONE = 15.0

def angdiff_deg(a, b):
    """Signed minimal a-b in (-180, 180]."""
    return ((a - b + 180.0) % 360.0) - 180.0

def _deadzone(c, dz=ARROW_STICK_DEADZONE):
    """Per-axis: subtract the dead-zone, keep sign, clamp at 0."""
    o = c - 128.0
    m = abs(o) - dz
    return 0.0 if m <= 0 else math.copysign(m, o)

def stick_angle_deg(sx, sy):
    """Stick direction in the decomp convention (deg), or None for neutral.
    0=down, 90=right, 180=up, 270=left. Per-axis dead-zoned. Slot-9 validated.

    Neutral (no swim input) uses the game's actual gate: mStickDistance =
    min(hypot(dz)/54, 1) <= 0.05  (d_a_player_main gates swim on mStickDistance > 0.05f).
    This supersedes the old square-deadzone test (both dz axes 0); they agree everywhere
    except a thin ring just outside the dz-15 square (0 < hypot <= 2.7), where the game
    blocks a tiny gain the square test would let through. Bit-identical to the gold stick
    table's `value <= 0.05` gate on all 65536 cells (verified), so no table dep needed.

    NOTE: this NAIVE per-axis atan2 is missing the octagonal clamp (see clamp_stick /
    main_stick_decode); it is exact only on-axis / inside the octagon. Off-axis near-full
    sticks need the clamped decode. Still used by swim + as the on-axis neutral gate."""
    ax, ay = _deadzone(sx), _deadzone(sy)
    if min(math.hypot(ax, ay) / 54.0, 1.0) <= 0.05:
        return None
    return math.degrees(math.atan2(ax, -ay)) % 360.0


# --- PADClamp octagon clamp + faithful main-stick decode (from decomp; Padclamp.c + CStick::update) ---
# Matches clean DTM, NOT the advancewith stick_angle_table.csv (why: knowledge/mechanics/walk-run.md).
_STICK_RAD2S = f32(10430.379)          # JUTGamePad::CStick::update: mAngle = (s16)(10430.379f * atan2f)


def clamp_stick(x, y, max_, xy, min_):
    """PADClamp ClampStick (Padclamp.c): per-axis sign-split + abs, per-axis dead-zone (subtract
    `min_`, floor 0), then an OCTAGONAL clamp -- a point outside the octagon (xy*max < d) is scaled
    onto the octagon edge by xy*max/d and each axis is integer-truncated to s8. `x,y` are signed
    (raw byte - 128). Returns the clamped signed (cx, cy). ClampRegion: main stick min=15/max=72/
    xy=40; sub (C-stick) min=15/max=59/xy=31."""
    sgx = 1 if x >= 0 else -1
    sgy = 1 if y >= 0 else -1
    x = -x if x < 0 else x
    y = -y if y < 0 else y
    x = 0 if x <= min_ else x - min_
    y = 0 if y <= min_ else y - min_
    if x == 0 and y == 0:
        return 0, 0
    if xy * y <= xy * x:
        d = xy * x + (max_ - xy) * y
    else:
        d = xy * y + (max_ - xy) * x
    if xy * max_ < d:
        x = int(xy * max_ * x / d)     # C integer division truncates toward zero
        y = int(xy * max_ * y / d)
    return sgx * x, sgy * y


def _clamped_angle_s16(cx, cy, clamp):
    """JUTGamePad::CStick::update (STICK_MODE_1) angle from a CLAMPED integer vector: normalize by
    `clamp` (54 main / 42 sub), cap magnitude at 1 (STICK_MODE_1 divides both axes by the magnitude),
    then mAngle = (s16)(10430.379f * atan2f(mPosX, -mPosY)). f32 throughout; s16 truncates toward 0."""
    px = f32(cx / clamp)
    py = f32(cy / clamp)
    value = f32(math.sqrt(f32(f32(px * px) + f32(py * py))))
    if value > 1.0:
        px = f32(px / value)
        py = f32(py / value)
    if py == 0.0:
        return 0x4000 if px > 0.0 else 0xC000           # +/-0x4000 special-case (CStick::update)
    return int(f32(_STICK_RAD2S * f32(math.atan2(px, -py)))) & 0xFFFF


def main_stick_decode(sx, sy):
    """Faithful (main-stick angle s16, mStickDistance) for a raw byte pair (0..255, center 128).
    Returns (None, msd) for a neutral stick (mStickDistance <= 0.05). The angle is the `mMainStickAngle`
    term in `m34DC = angle + 0x8000`; msd is `mStickDistance`. See clamp_stick / _clamped_angle_s16.

    msd uses the f64 `min(hypot(clamped)/54, 1)` form (== the swim/land magnitude): on-axis / inside
    the octagon the clamped vector equals the dead-zoned one, so msd + the neutral gate are byte-for-byte
    what the naive decode gave (on-axis goldens + locked on-axis live tests unchanged); off-axis it is
    corrected by the octagon clamp."""
    cx, cy = clamp_stick(sx - 128, sy - 128, 72, 40, 15)
    msd = min(math.hypot(cx, cy) / 54.0, 1.0)
    if msd <= 0.05:
        return None, msd
    return _clamped_angle_s16(cx, cy, 54.0), msd
