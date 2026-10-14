#!/usr/bin/env python3
"""
Change the base display step (digit) of an AM/PM/J balance by rescaling its type data.

EXPERIMENTAL. Tried on exactly one balance - a PM4600 taken from a 10 mg to a 1 mg digit,
displaying 5 mg steps - and on no other type. Read the caveats in the README before using it,
keep the original dump, and expect to need the bench to confirm the result.

The display step is 10^-(byte 0x2A) grams, and every type value is expressed in those steps,
so making the digit ten times finer means: decimals + 1, every weight-valued type pair x 10,
the internal multiplier (byte 0x14) reduced to keep the 24-bit values in range, SPAN/UCAL
scaled by the change in internal counts per gram, and the display deadband (byte 0x28, counted in
internal units) adjusted so it stays the same fraction of a digit.

Pair 0x17 is deliberately NOT scaled: it reads 900 steps on all five reference balances
(PM200, PM3000, PM4600, PM6000, PJ6000), i.e. it is specified in display steps, not in grams.

Usage:  rescale.py IN.BIN OUT.BIN --digit-mg 1 [--readability 5] [--scale-17]
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_eeprom as pm

GRAM_PAIRS = (0x11, 0x13, 0x15, 0x19, 0x1B, 0x1D, 0x21, 0x23, 0x25)   # values in grams -> scale
STEP_PAIRS = (0x17,)                                                   # values in display steps
LIMIT = 0x7FFFFF


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inp"); p.add_argument("out")
    p.add_argument("--digit-mg", type=float, required=True, help="wanted base display step in mg")
    p.add_argument("--readability", type=int, help="display step multiplier to store (1,2,5,10,...)")
    p.add_argument("--scale-17", action="store_true",
                   help="also scale pair 0x17 (keeps its value in grams instead of in steps)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    a = p.parse_args()

    x, swapped, _ = pm.load(a.inp)
    u_old, digit_old = pm.units(x)
    cap_g = pm.capacity_g(x)
    R = pm.raw_counts_per_g(x)
    span_old, ucal_old = pm.g24(x, 0x2E), pm.g24(x, 0x32)
    digit_new = a.digit_mg / 1000.0
    factor = digit_old / digit_new
    if abs(factor - round(factor)) > 1e-9 or round(factor) < 1:
        sys.exit(f"the step must become finer by a whole power of ten "
                 f"(now {digit_old*1000:g} mg, asked {a.digit_mg:g} mg)")
    factor = round(factor)
    decimals = round(-pm.__dict__.get("math", __import__("math")).log10(digit_new))

    print(f"{os.path.basename(a.inp)}: capacity {cap_g:g} g, step {digit_old*1000:g} mg -> "
          f"{a.digit_mg:g} mg (x{factor}),  A/D {R:.0f} counts/g")

    cap_steps = round(cap_g / digit_new)
    choice = None
    for mult_idx, mult in sorted(((i, m) for i, m in enumerate(pm.MULT76) if m),
                                 key=lambda t: -t[1]):
        span_new = span_old * (mult / digit_new) / u_old
        if cap_steps * mult <= LIMIT and span_new <= LIMIT:
            choice = (mult_idx, mult, span_new); break
    if not choice:
        sys.exit("impossible: no multiplier keeps both the internal value and SPAN inside 24 bits")
    mult_idx, mult, span_new = choice
    scale = (mult / digit_new) / u_old
    print(f"  internal multiplier {pm.MULT76[(x[0x14]>>4)&7]} -> {mult}, "
          f"full scale {cap_steps*mult/1e6:.2f}M (limit 8.39M)")
    print(f"  SPAN {span_old} -> {round(span_new)}, UCAL {ucal_old} -> {round(ucal_old*scale)}")

    # 1. decimals and multiplier
    old_mult_byte = x[0x14]
    x[0x2A] = (x[0x2A] & ~0x07) | (decimals & 7)
    x[0x14] = (x[0x14] & ~0x70) | (mult_idx << 4)
    # 2. type values
    pairs = GRAM_PAIRS + (STEP_PAIRS if a.scale_17 else ())
    for pair in pairs:
        val, step = pm.type_value(x, x[pair], x[pair + 1])
        if not val:
            continue
        r7, r6 = pm.encode_pair(val * factor, (x[pair + 1] >> 4) & 7, x[pair] & 0x80,
                                x[pair + 1] & 0x80)
        x[pair], x[pair + 1] = r7, r6
        print(f"  pair {pair:02X}: {val} -> {val*factor} steps ({val*digit_old:g} g, unchanged)")
    for pair in STEP_PAIRS:
        if not a.scale_17:
            val, _ = pm.type_value(x, x[pair], x[pair + 1])
            print(f"  pair {pair:02X}: {val} steps kept -> now {val*digit_new:g} g "
                  f"(was {val*digit_old:g} g)  <- watch this one")
    # 2b. display deadband (byte 0x28): held changes up to this many internal units. It is a fixed
    #     number of units, so keep it the same fraction of a digit: units/multiplier stays constant.
    #     The PM4600 (2 units, multiplier 8 = a quarter digit) went to 3 mg jumps when this was left alone.
    mult_old = pm.MULT76[(old_mult_byte >> 4) & 7]
    dead_old = x[0x28]
    dead_new = round(dead_old * mult / mult_old)
    x[0x28] = dead_new
    print(f"  display deadband (0x28): {dead_old} -> {dead_new} internal units "
          f"({dead_old*digit_old/mult_old*1000:g} mg -> {dead_new*digit_new/mult*1000:g} mg)")
    # 3. span and user trim
    pm.s24(x, 0x2E, round(span_new))
    pm.s24(x, 0x32, round(ucal_old * scale))
    # 4. readability, so the balance comes up at a step the A/D can actually resolve
    # default to the finest step still worth displaying: Mettler runs 1.5-3 A/D counts per step,
    # and about 0.75 is the point below which the digit is carried by dither alone
    want = a.readability or next(s for s in pm.STEP if R * digit_new * s / 10 >= 0.75)
    x[0x02] = (x[0x02] & ~0xE0) | (pm.STEP.index(want) << 5)
    print(f"  readability set to {want} -> display step {want*digit_new*1000:g} mg; "
          f"the menu can change this")

    # raw values only land on multiples of the normaliser (240 / the integration count in RAM 32h,
    # = 10 with stock firmware), so one real A/D count is that many raw units
    NORMALISER = 10
    counts = R * digit_new * want / NORMALISER
    print(f"  one A/D count = {NORMALISER/R*1000:.2f} mg, so that step is {counts:.2f} A/D counts"
          f"  (Mettler's own types run 1.5 to 3)")
    if counts < 1.0:
        print(f"\nWARNING: {counts:.2f} A/D counts per display step - finer than the converter resolves.\n"
              f"         It will still show a moving last digit, because the balance's own noise\n"
              f"         dithers it and the firmware averages, but there is no new information in it.\n"
              f"         A coarser --readability or a larger --digit-mg would give an honest display.")
    print("\nOn the bench: program this, set readability to the value that reproduces the balance's\n"
          "original step, and check a known weight reads as it did before. That tests the rescale\n"
          "on its own. Only then select the finer step.")
    if a.dry_run:
        print("\ndry run - nothing written")
        return
    pm.fix_checksums(x)
    pm.save(a.out, x, swapped)
    print(f"written {a.out}")


if __name__ == "__main__":
    main()
