#!/usr/bin/env python3
"""
Mettler AM/PM series (M-series, firmware V10.xx) NMC93C46 parameter EEPROM tool.

Reverse-engineered from ME-34172 V10.45 "Standard" cassette firmware (8051).
See EEPROM_MAP.md for the full description.

Byte order: the 8051 shifts each 16-bit EEPROM word out MSB first and stores it
at XRAM 0x7600+2n (high byte) / +2n+1 (low byte). Many programmers save the
93C46 in the opposite byte order. This tool auto-detects the order via the
checksums and always writes back in the same order it read.

Usage:
  pm_eeprom.py info   IN.BIN
  pm_eeprom.py set    IN.BIN OUT.BIN NAME=VALUE [NAME=VALUE ...]
  pm_eeprom.py lin    IN.BIN OUT.BIN --half-error G --load G [--capacity G]
  pm_eeprom.py tcspan IN.BIN OUT.BIN --ppm-per-c PPM
  pm_eeprom.py tczero IN.BIN OUT.BIN --g-per-c G [--capacity G]
  pm_eeprom.py sticker IN.BIN OUT.BIN V00 V01 ... V20     (write cell block from sticker)
  pm_eeprom.py calweight IN.BIN OUT.BIN --grams 2000
  pm_eeprom.py fine   IN.BIN OUT.BIN     (DeltaRange: fine display increment over the whole range)
  pm_eeprom.py fix    IN.BIN OUT.BIN                        (only recompute checksums)
"""
import argparse, sys


class PmError(Exception):
    """raised for anything the user can fix (bad file, impossible value)"""

# ---------------------------------------------------------------- layout
CK_SEED = 0xA6                       # sum(block) + 0xA6 == 0 (mod 256)
BLOCKS = {                           # name: (start, length) checksum byte = last
    "type":   (0x04, 0x2A),          # 0x04..0x2D  type parameters (type cassette)
    "span":   (0x2E, 0x04),          # 0x2E..0x31  factory span factor
    "usercal":(0x32, 0x04),          # 0x32..0x35  user calibration trim
    "cell":   (0x3E, 0x2A),          # 0x3E..0x67  cell parameters (= parameter sticker)
}
# 24-bit little-endian signed values in the cell block
CELL = {  # name: (addr, description)
    "X0":    (0x40, "raw reference point x0 (x = raw - X0)"),
    "T_LO":  (0x43, "temperature reading at lower TC step (info only)"),
    "T0":    (0x46, "reference temperature reading (middle TC step)"),
    "T_HI":  (0x49, "temperature reading at higher TC step (info only)"),
    "Z0":    (0x4C, "zero offset at T0                 [raw]"),
    "Z1":    (0x4F, "zero TC, linear                   [raw/2^16 per t]"),
    "Z2":    (0x52, "zero TC, quadratic                [raw/2^32 per t^2]"),
    "S0":    (0x55, "span correction at T0             [2^-24]"),
    "S1":    (0x58, "span TC, linear                   [2^-40 per t]"),
    "S2":    (0x5B, "span TC, quadratic                [2^-56 per t^2]"),
    "L0":    (0x5E, "linearity (x^2 term) at T0        [2^-48 per raw]"),
    "L1":    (0x61, "linearity TC, linear"),
    "L2":    (0x64, "linearity TC, quadratic"),
}
OTHER = {"SPAN": (0x2E, "factory span factor K (weight = W*K/2^23)"),
         "UCAL": (0x32, "user calibration trim added to K (|UCAL| <= SPAN/32)")}
T_COUNTS_PER_C = 2000                # temperature reading counts per degC (service manual)

# type-parameter decoding (firmware routine B845)
T1 = [100, 1000, 10000, 100000]; T2 = [0, 9, 90, 900]; STEP = [1, 2, 5, 10, 20, 50, 100, 200]
MULT76 = [8, 1, 2, 4, 8, 16, 32, 64]
NOMINAL_CAP = {0x20: 210, 0x30: 3100, 0x46: 4100, 0x60: 6100, 0x10: 110, 0x40: 410,
               0x12: 1200, 0x21: 2100}  # model code byte 0x05 -> capacity [g] (extend as needed)


def ck(x, a, n):
    return (CK_SEED + sum(x[a:a + n])) & 0xFF


def load(path):
    raw = bytearray(open(path, "rb").read())
    if len(raw) != 128:
        raise PmError(f"{path}: expected 128 bytes (93C46 x16), got {len(raw)}")
    swapped = bytearray(raw[i ^ 1] for i in range(128))
    def score(x): return sum(ck(x, a, n) == 0 for a, n in BLOCKS.values()) + ((x[0x68] & 0xF8) == 0x60)
    s_raw, s_sw = score(raw), score(swapped)
    if s_sw >= s_raw:
        return swapped, True, s_sw
    return raw, False, s_raw


def save(path, x, swapped):
    out = bytes(x[i ^ 1] for i in range(128)) if swapped else bytes(x)
    open(path, "wb").write(out)


def g24(x, a):
    return int.from_bytes(x[a:a + 3], "little", signed=True)


def s24(x, a, v):
    if not -0x800000 <= v <= 0x7FFFFF:
        raise PmError(f"value {v} out of signed 24-bit range")
    x[a:a + 3] = v.to_bytes(3, "little", signed=True)


def fix_checksums(x):
    for a, n in BLOCKS.values():
        last = a + n - 1
        x[last] = (-(CK_SEED + sum(x[a:last]))) & 0xFF


def type_value(x, r7, r6):
    return T1[r6 & 3] * (r7 & 0x7F) + T2[(r6 >> 2) & 3], STEP[(r6 >> 4) & 7]


def units(x, capacity_g=None):
    """internal weight units per gram, derived from the type block"""
    cap_digits, _ = type_value(x, x[0x15], x[0x16])
    cap_nominal = T1[x[0x16] & 3] * (x[0x15] & 0x7F)
    mult = MULT76[(x[0x14] >> 4) & 7]   # internal units per display digit (firmware B845/B8C8;
                                        # the service factor is NOT applied, ROM 8009 bit7 = 1)
    if capacity_g is None:
        capacity_g = NOMINAL_CAP.get(x[0x05])
    if capacity_g is None:
        return None, None
    digit_g = capacity_g / cap_nominal
    return mult / digit_g, digit_g


def sticker_words(x):
    """the 21 values on the parameter sticker (without the check nibble)"""
    return [x[0x3E + 2 * i + 1] << 8 | x[0x3E + 2 * i] for i in range(21)]


def raw_counts_per_g(x, capacity_g=None):
    u, _ = units(x, capacity_g)
    K = g24(x, 0x2E) + g24(x, 0x32)
    return None if u is None else u * 2 ** 23 / K


# ---------------------------------------------------------------- commands
def cmd_info(x, swapped, args):
    print(f"byte order in file : {'swapped (programmer LE)' if swapped else '8051 order'}")
    print(f"model code  [05]   : 0x{x[5]:02X}    type-check [68]: 0x{x[0x68]:02X}")
    print(f"ID          [0E-10]: {x[0x0E]:02X}{x[0x0F]:02X}{x[0x10]:02X}")
    for name, (a, n) in BLOCKS.items():
        print(f"checksum {name:8s}: {'OK' if ck(x, a, n) == 0 else 'BAD'}  ({a:02X}..{a + n - 1:02X})")
    cap, _ = type_value(x, x[0x15], x[0x16])
    cal, _ = type_value(x, x[0x25], x[0x26])
    _, sf = type_value(x, x[0x11], x[0x12])
    print(f"type: capacity {cap} digits, cal weight {cal} digits, service factor {sf}")
    u, digit = units(x, args.capacity)
    print()
    for name, (a, d) in {**OTHER, **CELL}.items():
        print(f"  {name:5s} [{a:02X}] {g24(x, a):9d}   {d}")
    print(f"  shift cfg [3E..3F] = {x[0x3E]:02X} {x[0x3F]:02X} (standard BA AE)")
    print()
    T = T_COUNTS_PER_C
    print("Derived:")
    print(f"  TC steps: {(g24(x,0x43)-g24(x,0x46))/T:+.1f} / 0 / {(g24(x,0x49)-g24(x,0x46))/T:+.1f} degC around T0")
    print(f"  span TC compensation at T0 : {g24(x,0x58)*T/2**40*1e6:+.1f} ppm/degC")
    if u:
        R = raw_counts_per_g(x, args.capacity)
        print(f"  digit {digit:g} g, internal units/g {u:g}, ~raw counts/g {R:.0f} (estimate)")
        print(f"  zero TC compensation at T0 : {g24(x,0x4F)*T/2**16/R*1000:+.2f} mg/degC (estimate)")
        capg = args.capacity or NOMINAL_CAP[x[5]]
        Rf = R * capg
        print(f"  linearity bow (L0) at half capacity after 2-point cal: "
              f"{-g24(x,0x5E)*Rf*Rf/4/2**48/R*1000:+.2f} mg (estimate)")
    print()
    print("Sticker values (lines 00-20, low 16 bits; check nibble not computed):")
    print("  " + " ".join(f"{i:02d}:x{w:04X}" for i, w in enumerate(sticker_words(x))))


def need_R(x, args):
    if args.raw_per_g:
        return args.raw_per_g
    R = raw_counts_per_g(x, args.capacity)
    if R is None:
        sys.exit("unknown model code, pass --capacity <grams>")
    return R


def apply(x, name, delta):
    a = CELL[name][0]
    old = g24(x, a); s24(x, a, old + delta)
    print(f"{name}: {old} -> {old + delta}  (delta {delta:+d})")


def cmd_set(x, args):
    for kv in args.assign:
        k, v = kv.split("="); k = k.upper()
        a = CELL[k][0] if k in CELL else OTHER[k][0]
        print(f"{k}: {g24(x, a)} -> {int(v, 0)}"); s24(x, a, int(v, 0))


def cmd_lin(x, args):
    R = need_R(x, args)
    Rf = R * args.load
    d = round(4 * (args.half_error / args.load) * 2 ** 48 / Rf)
    print(f"using ~{R:.0f} raw counts/g; expected change of half-load reading after recal: {-args.half_error:+g} g")
    apply(x, "L0", d)
    print("Recalibrate afterwards, then re-check 1/4, 1/2, 3/4 load.")


def cmd_tcspan(x, args):
    d = round(-args.ppm_per_c * 1e-6 * 2 ** 40 / T_COUNTS_PER_C)
    apply(x, "S1", d)


def cmd_tczero(x, args):
    R = need_R(x, args)
    print(f"using ~{R:.0f} raw counts/g")
    d = round(-args.g_per_c * R * 2 ** 16 / T_COUNTS_PER_C)
    apply(x, "Z1", d)


def encode_pair(value, step_idx, flag):
    """pack a value back into the 2-byte type code used by firmware B845"""
    for d in range(4):
        for o in range(4):
            rest = value - T2[o]
            if rest > 0 and rest % T1[d] == 0 and 1 <= rest // T1[d] <= 127:
                return (rest // T1[d]) | (0x80 if flag else 0), (step_idx << 4) | (o << 2) | d
    raise PmError(f"value {value} cannot be encoded as a type value")


def cmd_calweight(x, args):
    u, digit = units(x, args.capacity)
    if u is None:
        sys.exit("unknown model code, pass --capacity <grams>")
    old, step = type_value(x, x[0x25], x[0x26])
    new = round(args.grams / digit)
    if abs(new * digit - args.grams) > digit / 2:
        sys.exit(f"{args.grams} g is not a whole number of {digit} g digits")
    r7, r6 = encode_pair(new, (x[0x26] >> 4) & 7, x[0x25] & 0x80)
    x[0x25], x[0x26] = r7, r6
    print(f"calibration weight: {old * digit:g} g -> {new * digit:g} g "
          f"({old} -> {new} digits, bytes 25/26 = {r7:02X} {r6:02X})")
    print(f"the balance will accept a weight within about +/-{new * digit / 32:g} g of that")


def cmd_fine(x, args):
    """DeltaRange models: make the fine (small) display increment apply over the whole range."""
    changed = False
    for pair in (0x21, 0x23):
        r7, r6 = x[pair], x[pair + 1]
        val, step = type_value(x, r7, r6)
        if val and step > 1:
            x[pair + 1] = r6 & 0x8F          # step index -> 0 (increment 1)
            print(f"range block at {pair:02X}: limit {val} digits, increment {step} -> 1")
            changed = True
    if not changed:
        sys.exit("no coarse secondary range found - this is not a DeltaRange type "
                 "(the display already uses its finest increment)")


def cmd_sticker(x, args):
    vals = [int(v) for v in args.values]
    if len(vals) != 21:
        sys.exit("need 21 sticker values (lines 00..20)")
    for i, v in enumerate(vals):
        w = v & 0xFFFF
        x[0x3E + 2 * i] = w & 0xFF; x[0x3E + 2 * i + 1] = w >> 8
    if ck(x, 0x3E, 0x2A) != 0:
        print("WARNING: cell block checksum from sticker is not 0 - check for typos! (fixing anyway)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    def mk(name, out=True):
        q = sp.add_parser(name); q.add_argument("inp")
        if out: q.add_argument("out")
        q.add_argument("--capacity", type=float, help="nominal capacity in g (if model unknown)")
        q.add_argument("--raw-per-g", type=float, help="override estimated raw counts per gram")
        return q
    mk("info", out=False)
    mk("set").add_argument("assign", nargs="+")
    q = mk("lin"); q.add_argument("--half-error", type=float, required=True,
                                  help="reading - true value at half load [g] (after zero/span cal)")
    q.add_argument("--load", type=float, required=True, help="full test load used for span [g]")
    mk("tcspan").add_argument("--ppm-per-c", type=float, required=True,
                              help="observed full-load drift, +ppm/degC means reading rises when warmer")
    mk("tczero").add_argument("--g-per-c", type=float, required=True,
                              help="observed zero drift [g/degC], + means reading rises when warmer")
    mk("sticker").add_argument("values", nargs="+")
    mk("fine")
    mk("calweight").add_argument("--grams", type=float, required=True,
                                 help="calibration weight to store, in grams")
    mk("fix")
    args = p.parse_args()
    try:
        x, swapped, score = load(args.inp)
    except PmError as e:
        sys.exit(str(e))
    if args.cmd == "info":
        return cmd_info(x, swapped, args)
    try:
        {"set": cmd_set, "lin": cmd_lin, "tcspan": cmd_tcspan, "tczero": cmd_tczero,
         "sticker": cmd_sticker, "fine": cmd_fine, "calweight": cmd_calweight,
         "fix": lambda x, a: None}[args.cmd](x, args)
    except PmError as e:
        sys.exit(str(e))
    fix_checksums(x)
    save(args.out, x, swapped)
    print(f"written {args.out} (checksums updated, {'swapped' if swapped else '8051'} byte order)")


if __name__ == "__main__":
    main()
