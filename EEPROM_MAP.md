# Mettler AM/PM (M-series) – NMC93C46 parameter EEPROM

Reverse-engineered from firmware **ME-34172 V10.45 "Standard"** (8051, cassette ROM mapped at 0x8000)
and verified against 4 dumps + parameter stickers (PM200, PM3000, PM4600, PM6000).

## 1. Hardware / byte order

* 93C46 in x16 mode, bit-banged on P1: **CS = P1.2, SK = P1.0, DI = P1.1, DO = P1.7** (driver at 0xCE05–0xCF50).
* Firmware keeps a RAM mirror at XRAM 0x7600–0x767F. Word *n* → XRAM[2n] = MSB, XRAM[2n+1] = LSB.
* **The .BIN files from your programmer are byte-swapped** relative to that (every byte pair reversed).
  All addresses below are in *firmware (XRAM) order*; `pm_eeprom.py` converts automatically.
* Multi-byte values are **little-endian, signed 24-bit** (firmware math library 0xDA8F–0xDCC5).

## 2. Map (firmware order)

| Addr | Content |
|---|---|
| 00–03 | misc config (not checksummed) |
| 04–2D | **type block** (from type cassette); checksum byte 2D |
| 05 | model code (0x20 PM200, 0x30 PM3000, 0x46 PM4600, 0x60 PM6000) |
| 0E–10 | **ID number, BCD** (= "ID" line on sticker, e.g. 69 43 37) |
| 11–26 | weighing-range values (capacity, cal weight, steps …) as 2-byte codes (see §5) |
| 2E–31 | **SPAN** – factory span factor K (24 bit) + checksum 31 |
| 32–35 | **UCAL** – user calibration trim (24 bit) + checksum 35; written by the normal CAL key |
| 2A | **decimals shown in grams** - this is what fixes the display step (3 = 1 mg, 2 = 10 mg, 1 = 0.1 g) |
| 36–3D | configuration (menu settings) |
| 3E–67 | **cell block = the 21 sticker lines**; checksum byte 67 |
| 68 | type check: `0x60 | code`, code (3 bit) derived from bytes 16, 21–24 (firmware 0xB233) |
| 6C–7F | other/config |

**Checksum** (routine 0xCBA3): `(0xA6 + Σ bytes of block) mod 256 == 0`, last byte of each block is the
check byte. Wrong cell/type/span checksum → error at power-up (internal error code 6); wrong 0x68 → code 8/9.

### Cell block (sticker)

The sticker lines 00…20 are the 16-bit words at 3E, 40, … 66 (value = byte[2n+1]·256 + byte[2n]).
The printed number is 20 bits: **top 4 bits = a check digit** added by the ServicePac (not computed by
the standard firmware; algorithm not recovered), low 16 bits = EEPROM word.
Example: PM200 line 00 = 44730 = 0x0AEBA → word 0xAEBA.
So you can **restore a cell block from the sticker** with `pm_eeprom.py sticker` (verified bit-exact on all 4 units).

The firmware actually reads that block as 24-bit numbers:

| Addr | Name | Meaning |
|---|---|---|
| 3E–3F | – | shift configuration (always BA AE) |
| 40 | X0 | raw reference, x = raw − X0 (3934080 on all units) |
| 43 / 46 / 49 | T_LO / **T0** / T_HI | temperature readings of the 3 TC steps; T0 = reference |
| 4C 4F 52 | Z0 Z1 Z2 | zero point vs temperature |
| 55 58 5B | S0 S1 S2 | span (sensitivity) vs temperature |
| 5E 61 64 | L0 L1 L2 | linearity (quadratic term) vs temperature |
| 67 | – | checksum |

## 3. Weighing formula (routine 0xC919 → 0x841C)

```
t  = Traw − T0                          (temperature counts, ~2000 counts/°C)
Z  = Z0 + ((Z1 + (Z2·t >> 16))·t >> 16)
S  = S0 + ((S1 + (S2·t >> 16))·t >> 16)
L  = L0 + ((L1 + (L2·t >> 16))·t >> 16)
x  = raw − X0
W  = raw + Z + ((S + (L·x >> 24))·x >> 24)
weight = W · (SPAN + UCAL) >> 23          (then zero/tare, filtering, display rounding)
```

This is the standard Mettler MFR correction: a quadratic in load, with each coefficient quadratic in temperature.

## 4. What to change

| Problem | Parameter | Effect |
|---|---|---|
| Linearity (error at ½ load after zero/span cal) | **L0** | ΔL0 > 0 lowers the mid-range reading |
| Span drift with temperature | **S1** | span change = ΔS1·2000/2⁴⁰ per °C ≈ **−550 per +1 ppm/°C** of observed drift |
| Zero drift with temperature | **Z1** | needs raw-counts/g (estimated) |
| Linearity changes with temperature | L1 | rarely needed |

Don't change X0, T0, the 3E/3F shift bytes, or the type block. Changing T0 moves the reference point of every TC polynomial.

### Linearity procedure
1. Warm up, calibrate (normal CAL with the standard cassette).
2. Load the full test weight F and zero, then read the half load: e = reading − true value.
3. `pm_eeprom.py lin IN.BIN OUT.BIN --half-error e --load F`, program it, recalibrate, and check again at ¼, ½ and ¾ load.
4. The gram→count scale is derived from the type block (internal units per display digit = 8, byte 0x14).
   Cross-check: it gives 6.30 / 4.79 / 6.35 / 6.38 million raw counts at capacity for the PM200 / PM3000 /
   PM4600 / PM6000 — i.e. all four cells use nearly the same full-scale raw span, which is a good sign.
   Still, treat the first step as a trial. If it gives Δe_observed instead of the expected −e, correct the scale:
   `--raw-per-g (old_value · e / −Δe_observed)`, or simply scale the next ΔL0 by e_remaining/Δe_observed.

### Span TC procedure (does not depend on the gram scale)
Measure the full-load reading at two stable temperatures (thermometer inside the housing, several hours each).
drift_ppm = (R_warm − R_cold)/F/ΔT·1e6 → `pm_eeprom.py tcspan IN OUT --ppm-per-c drift_ppm`.
At T0 the reading doesn't change, so no recalibration is needed.

### Zero TC
`pm_eeprom.py tczero IN OUT --g-per-c z` (z = zero drift, + means it reads higher when warm). Uses the estimated scale.

## 5. Type values (routine 0xB845)
2-byte code (b0, b1): value = [100,1000,10000,100000][b1&3] · (b0&0x7F) + [0,9,90,900][(b1>>2)&3],
step = [1,2,5,10,20,50,100,200][(b1>>4)&7]; internal value ×8 (multiplier table [8,1,2,4,8,16,32,64]
indexed by byte 0x14 bits 4-6) — i.e. **8 internal counts per displayed digit**. Routine 0xB8C8 would
additionally multiply by the service factor, but only when ROM byte 0x8009 bit 7 = 0; in the standard
cassette 0x8009 = 0xFF, so it does not (see §7).
The display step in grams is **10^-(byte 0x2A)**, so capacity and calibration weight follow from the
type data alone - no table of models is needed, and types that share a model code (the PM6000 and the
PJ6000 are both 0x60, at 6100 g and 6000 g) come out right.
Pair 15/16 = capacity in digits (PM200: 210090 = 210 g in mg + 90 d overload), 25/26 = calibration weight
(PM200 100000 mg, PM4600 100000 ×10 mg, PM6000 20000 ×0.1 g), step of 11/12 = the manual's "Service Factor".

## 6. Tool

```
python3 tools/pm_eeprom.py info  "src/NMC93C46 PM200.BIN"
python3 tools/pm_eeprom.py lin   IN.BIN OUT.BIN --half-error 0.002 --load 200
python3 tools/pm_eeprom.py tcspan IN.BIN OUT.BIN --ppm-per-c 5
python3 tools/pm_eeprom.py set   IN.BIN OUT.BIN L0=-1500 S1=-182000
python3 tools/pm_eeprom.py sticker IN.BIN OUT.BIN 44730 984960 ... (21 values)
python3 tools/pm_eeprom.py fix   IN.BIN OUT.BIN
```
All write commands recompute every checksum and keep the file's byte order. Keep the original dump!

## 7. Firmware notes (V10.45 cassette ROM, 27C256 at 0x8000)

* **ROM self-test** (0x8ABC, called at power-up): the sum of all 32768 bytes must be 0 (mod 256), else
  error code 3. It is skipped if ROM byte 0x8006 == 0x55 (it is 0xFF here, so the test runs).
  Any patch must therefore be compensated in a filler byte (the image has 6739 0xFF bytes).
* **0x8009 = feature flags** (0xFF in the standard cassette). Bit 7 distinguishes the standard cassette from
  the type/service cassettes: when it is 0, routine 0xB8C8 multiplies every type value (capacity, calibration
  weight, display step, limits) by the **service factor** from type pair 0x11/0x12 — this is what makes the
  ServicePac adjustment cassette display one more digit (manual: "with the adjustment cassette the readability
  is higher by this factor"). Bit 6 enables the 0x68 type check, bit 1 is tested in the interface parser.
* **Display resolution budget**: 8 internal counts per displayed digit, and the ADC gives roughly
  30 raw counts per digit on the PM200, 15 on the PM4600, 10 on the PM6000 and 15 on the PM3000.
  So about one extra digit of *numeric* resolution exists, no more.
* **Display width**: the VFD has 7 cells; the weight field is 6 digits plus the decimal point.

## 8. Display increment and decimal point

The display increment is the byte at internal RAM 75h: the firmware takes the **largest** `step` of the
range blocks that currently apply (0x8691 for the base increment, 0x86A2/0x86AB for the secondary ranges)
and passes it to the formatter via XRAM 0x74E4. The **decimal point** is independent of it: it comes from
**EEPROM byte 0x2A** (decimals in g: PM200 = 3, PM4600 = 2, PM3000/PM6000 = 1) plus an offset from the unit
table selected by the unit code in byte 0x03. The weight field is 6 digits plus the point, in a 7-cell VFD.

**DeltaRange types (PM4600, PM4800, PM460, PM480, PM2500).** Type pair 0x21 holds the fine-range limit
together with the increment used above it: on the PM4600, 60000 steps (= 600.00 g) with increment 10, which
is what turns the 10 mg digit into 100 mg above 600 g. Setting that increment to 1 (`pm_eeprom.py fine`)
gives **10 mg over the whole range** - 4100.90 g, six digits plus the point, exactly filling the display.
Byte 0x2A already says 2 decimals, so the decimal point does not move. One data byte changes, plus the block
checksum. Headroom: 410090 steps x 8 = 3.28M, well inside the 24-bit value, and the A/D delivers about
15 raw counts per 10 mg step.

A PM4600 changed this way matches the PM4000 specification: same 4100 g capacity, same 10 mg readability,
same 30 mg cornerload tolerance. Mettler sells the same cell both ways.

**Non-DeltaRange types** (PM200, PM3000, PM6000) have no secondary range. There the extra resolution is the
service factor in pair 0x11 (x2, x10, x2), which the standard firmware ignores - reaching it means changing
the firmware, which is out of scope here. For a x10 factor the decimals byte 0x2A has to be raised with it,
since the decimal point does not follow by itself.

**How far it can go.** Resolution is bounded by the measuring cell, not by the format: the A/D accumulates
over the cycle count in RAM 32h and is normalised by 240/32h, giving raw steps of about 6.5 mg on a PM4600,
and the specified repeatability of these cells is one display step. Asking for ten times the resolution
produces a digit that shows air currents, not mass.
