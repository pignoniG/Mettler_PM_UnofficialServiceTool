"""
Published adjustment tolerances and reference values of the AM/PM series, from the service manual's data section
(linearity, calibration weights, temperature compensation, calibration resistor).  Specifications only.

Weights in grams, tolerances in milligrams, resistor in ohms.
  lin_up / lin_down   admissible display error at 1/4, 1/2 and 3/4 load, loading / unloading
  cal_normal / cal_certified / cal_service   calibration weight of the normal and certified balances, and for the
                      service (adjustment) module, which is the full load
  tc_zero / tc_sens   admissible zero-point / sensitivity change over 10 degC, in mg
  r_ref               reference resistor(s) of the resistance adjustment, with the resistor's temperature coefficient
"""

# name: (lin_up, lin_down, cal_normal, cal_certified, cal_service, tc_zero, tc_sens, r_ref ohms, ppm/degC)
_ROWS = {
    "PM100":    (2, 2, 100, 100, 100, 20, 4, (1850,), 5),
    "PM200":    (2, 2, 100, 200, 200, 20, 8, (1850,), 5),
    "PM300":    (3, 4, 100, 300, 300, 20, 12, (800,), 5),
    "PM400":    (2, 4, 200, 200, 400, 20, 16, (800,), 5),
    "PM460":    (3, 4, 100, 200, 400, 20, 16, (800,), 5),
    "PM480":    (2, 4, 100, 200, 400, 20, 16, (800,), 5),
    "PM600":    (10, 15, 500, 500, 500, 200, 30, (680,), 5),
    "PM1200":   (2, 4, 1000, 1000, 1200, 25, 25, (480, 520), 1),
    "PM2000":   (20, 20, 1000, 2000, 2000, 200, 80, (680,), 5),
    "PM2500":   (2, 4, 1000, 1000, 2000, 40, 40, (420, 440, 480), 1),
    "PM3000":   (30, 40, 1000, 2000, 3000, 200, 200, (530,), 5),
    "PM4000":   (20, 40, 2000, 2000, 4000, 200, 180, (530,), 5),
    "PM4600":   (30, 40, 1000, 2000, 4000, 200, 180, (530,), 5),
    "PM4800":   (20, 40, 1000, 2000, 4000, 200, 180, (530,), 5),
    "PM6000":   (200, 200, 2000, 4000, 6000, 1000, 400, (480,), 5),
    "PM6100":   (20, 40, 4000, 4000, 6000, 300, 220, (400, 420, 440), 1),
    "PM6":      (200, 250, 2000, 4000, 6000, 600, 300, (480,), 5),
    "AM50":     (0.2, 0.4, 50, 50, 50, 2.5, 1, (840, 880, 920), 1),
    "AM100":    (0.2, 0.4, 100, 100, 100, 3.5, 2.5, (840, 880, 920), 1),
}
_KEYS = ("lin_up", "lin_down", "cal_normal", "cal_certified", "cal_service", "tc_zero", "tc_sens", "r_ref", "r_ppm")

SPECS = {name: dict(zip(_KEYS, row)) for name, row in _ROWS.items()}

# the resistance check accepts this much calibration error before the resistor has to be changed
RESISTOR_LIMIT_PCT = {"PM": 1.25, "AM": 5.0}


def resistor_limit(name):
    return RESISTOR_LIMIT_PCT["AM" if name.startswith(("AM", "CM", "AJ", "CJ")) else "PM"]


def guess_model(capacity_g):
    """default model for a capacity in grams (None if no model has that capacity)"""
    for name, s in SPECS.items():
        if name.startswith("PM") and abs(s["cal_service"] - capacity_g) < 1e-6:
            return name
    return None
