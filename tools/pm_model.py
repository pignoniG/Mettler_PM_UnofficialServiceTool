"""
The balance's weighing formula and the adjustment calculations of the service manual, solved for the EEPROM
parameters.  No user interface and no serial access: values in, values out.

Formula (EEPROM_MAP.md section 3, checked exactly against the firmware):
    t  = Traw - T0                       (temperature counts, 2000 per degC)
    Z  = Z0 + ((Z1 + (Z2*t >> 16))*t >> 16)          zero
    S  = S0 + ((S1 + (S2*t >> 16))*t >> 16)          span, relative to x
    L  = L0 + ((L1 + (L2*t >> 16))*t >> 16)          linearity
    x  = raw - X0
    W  = raw + Z + ((S + (L*x >> 24))*x >> 24)
    weight = W * (SPAN + UCAL) >> 23                 internal units; units(x)[0] of them per gram
"""
import pm_eeprom as pm

T_PER_C = pm.T_COUNTS_PER_C
Z_AT, S_AT, L_AT = (0x4C, 0x4F, 0x52), (0x55, 0x58, 0x5B), (0x5E, 0x61, 0x64)
T0_AT, X0_AT, SPAN_AT, UCAL_AT = 0x46, 0x40, 0x2E, 0x32


class ModelError(Exception):
    pass


def _poly(x, at, t):
    t = int(round(t))                    # averaged readings are fractional; the firmware works in integers
    a, b, c = (pm.g24(x, i) for i in at)
    return a + ((b + ((c * t) >> 16)) * t >> 16)


def internal_weight(x, raw, temp, ucal=None):
    """what the firmware computes before tare and display rounding (internal units)"""
    raw, temp = int(round(raw)), int(round(temp))
    t = temp - pm.g24(x, T0_AT)
    Z, S, L = _poly(x, Z_AT, t), _poly(x, S_AT, t), _poly(x, L_AT, t)
    xr = raw - pm.g24(x, X0_AT)
    W = raw + Z + (((S + ((L * xr) >> 24)) * xr) >> 24)
    k = pm.g24(x, SPAN_AT) + (pm.g24(x, UCAL_AT) if ucal is None else ucal)
    return W * k >> 23


def weight_g(x, raw, temp, ucal=None):
    return internal_weight(x, raw, temp, ucal) / pm.units(x)[0]


def indicated_g(x, raw, temp, raw0, temp0, ucal=None):
    """tared reading in grams: the weight at (raw, temp) minus the weight of the empty pan at (raw0, temp0)"""
    return weight_g(x, raw, temp, ucal) - weight_g(x, raw0, temp0, ucal)


# ------------------------------------------------------------ calibration (service manual 4.2.5)
def calibration(x, raw, temp, raw0, temp0, true_g):
    """-> (new UCAL, indicated g now, UCAL limit).  The user calibration trims the span factor so that the
    tared reading at the calibration weight equals the weight."""
    ind = indicated_g(x, raw, temp, raw0, temp0)
    if ind <= 0:
        raise ModelError("the loaded reading is not above the zero reading: check the load and the capture")
    span, ucal = pm.g24(x, SPAN_AT), pm.g24(x, UCAL_AT)
    k_new = (span + ucal) * true_g / ind
    return round(k_new - span), ind, span // 32


def span_error_pct(x, raw, temp, raw0, temp0, true_g):
    """reading error at the calibration weight with the factory span only (UCAL = 0), in percent: what the
    calibration resistor has to bring inside the limit (manual 4.2.2)"""
    ind = indicated_g(x, raw, temp, raw0, temp0, ucal=0)
    return (ind - true_g) / true_g * 100.0, ind


# ------------------------------------------------------------ resistance adjustment (manual 4.2.3)
def r42_from_error(true_g, r_ref, error_g):
    """manual formula: R42 = loaded weight * reference resistance / error  (error = reading - weight without R42)"""
    if error_g <= 0:
        raise ModelError("the error must be positive: the reading has to be above the weight without the resistor")
    return true_g * r_ref / error_g


def r42_correction(true_g, r_ref, r_old, residual_g):
    """R42 to fit instead of r_old when the reading with it fitted is residual_g above the weight.  Follows from the
    formula, assuming R42 lowers the reading by true_g * r_ref / R42: 1/R_new = 1/R_old + residual/(true_g * r_ref)."""
    inv = 1.0 / r_old + residual_g / (true_g * r_ref)
    if inv <= 0:
        raise ModelError("the reading is too low to be corrected by a smaller resistor: R42 would have to be removed or larger")
    return 1.0 / inv


# ------------------------------------------------------------ linearity (manual 4.2.4)
def linearity(x, points):
    """points: [(true g, raw, temp)], the first with true g == 0 (the tare).  Least squares on
        error_i = a*indicated_i + b*d_i,   d_i = d(indicated)/d(L0),
    gain a is what the following calibration absorbs; the L0 change is -b.
    -> dict(delta_l0, before [g per point], after [g per point], new_l0)"""
    if len(points) < 3 or points[0][0] != 0:
        raise ModelError("need the zero point and at least two loads (four or five are better)")
    m0, raw0, t0 = points[0]
    base = bytearray(x)
    trial = bytearray(x)
    l0 = pm.g24(x, L_AT[0])
    pm.s24(trial, L_AT[0], l0 + 1000)

    def ind(img, p):
        return indicated_g(img, p[1], p[2], raw0, t0)
    rows = []
    for p in points[1:]:
        i = ind(base, p)
        rows.append((i, (ind(trial, p) - i) / 1000.0, i - p[0]))
    # normal equations of the two-parameter fit
    s11 = sum(r[0] * r[0] for r in rows); s12 = sum(r[0] * r[1] for r in rows); s22 = sum(r[1] * r[1] for r in rows)
    y1 = sum(r[0] * r[2] for r in rows); y2 = sum(r[1] * r[2] for r in rows)
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-30:
        raise ModelError("the loads are not distinct enough to separate gain and curvature")
    a = (y1 * s22 - y2 * s12) / det
    b = (s11 * y2 - s12 * y1) / det
    before = [0.0] + [r[2] for r in rows]
    after = [0.0] + [r[2] - a * r[0] - b * r[1] for r in rows]
    return dict(delta_l0=round(-b), new_l0=l0 + round(-b), before=before, after=after, gain=a)


def linearity_combinations(x, zero, configs, n_weights):
    """Linearity from weights whose masses need not be known (only constant).
    zero: (raw, temp) of the empty pan.  configs: [(set of weight indices, raw, temp)], every weight used by at least
    one configuration.  Unknowns are the masses m_j (in the units of the present calibration) and the change dL of L0:
        indicated_c + dL * d_c = sum of m_j in c
    solved by least squares; the gain error is inside the masses, so no calibration weight is needed.
    -> dict(delta_l0, new_l0, masses [g], before, after [g per configuration], condition note)"""
    raw0, t0 = zero
    if len(configs) < n_weights + 2:
        raise ModelError("need at least %d configurations for %d weights (each alone, then growing sums)" % (n_weights + 2, n_weights))
    if any(not c[0] for c in configs):
        raise ModelError("a configuration needs at least one weight")
    trial = bytearray(x)
    l0 = pm.g24(x, L_AT[0])
    pm.s24(trial, L_AT[0], l0 + 1000)
    ind = [indicated_g(x, c[1], c[2], raw0, t0) for c in configs]
    d = [(indicated_g(trial, c[1], c[2], raw0, t0) - i) / 1000.0 for c, i in zip(configs, ind)]
    mass_cols = [[-1.0 if j in c[0] else 0.0 for c in configs] for j in range(n_weights)]
    if any(not any(col) for col in mass_cols):
        raise ModelError("a weight is not used in any configuration")
    rhs = [-i for i in ind]
    try:
        sol = _lsq([d] + mass_cols, rhs)
        sol0 = _lsq(mass_cols, rhs)
    except ModelError:
        raise ModelError("the combinations cannot separate the weights from the curvature: use each weight alone and growing sums")
    dl, masses = sol[0], sol[1:]
    res = lambda dl_, ms: [i + dl_ * di - sum(ms[j] for j in c[0]) for c, i, di in zip(configs, ind, d)]
    return dict(delta_l0=round(dl), new_l0=l0 + round(dl), masses=masses,
                before=res(0.0, sol0), after=res(dl, masses))


# ------------------------------------------------------------ temperature compensation (manual 4.2.6, 4.2.7)
def drift_per_c(temp_a, temp_b, zero_a, zero_b, full_a, full_b):
    """service manual 4.2.6: zero drift and sensitivity drift in g per degC from two warm-up measurements
    (readings in grams, temperatures in counts)"""
    dt = (temp_b - temp_a) / T_PER_C
    if dt == 0:
        raise ModelError("the two temperatures are the same")
    return (zero_b - zero_a) / dt, (full_b - full_a) / dt, dt


def _lsq(cols, y):
    """small dense least squares via normal equations (columns are lists)"""
    n = len(cols)
    A = [[sum(ci * cj for ci, cj in zip(cols[i], cols[j])) for j in range(n)] for i in range(n)]
    b = [sum(ci * yi for ci, yi in zip(cols[i], y)) for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(A[r][i]))
        if abs(A[p][i]) < 1e-18:
            raise ModelError("the temperatures are too close together to fit a curve")
        A[i], A[p] = A[p], A[i]; b[i], b[p] = b[p], b[i]
        for r in range(i + 1, n):
            f = A[r][i] / A[i][i]
            for c in range(i, n):
                A[r][c] -= f * A[i][c]
            b[r] -= f * b[i]
    sol = [0.0] * n
    for i in reversed(range(n)):
        sol[i] = (b[i] - sum(A[i][j] * sol[j] for j in range(i + 1, n))) / A[i][i]
    return sol


def fit_poly(ts, vals, order):
    """c0 + c1*t/2^16 + c2*t^2/2^32 (the units the EEPROM uses) through the points, least squares, order 0..2"""
    cols = [[1.0] * len(ts)]
    if order >= 1:
        cols.append([t / 65536.0 for t in ts])
    if order >= 2:
        cols.append([t * t / 4294967296.0 for t in ts])
    c = _lsq(cols, vals)
    return c + [0.0] * (3 - len(c))


class TcPoint:
    """one temperature step: zero reading and, optionally, the reading under a known load (raw values, temperature count)"""
    def __init__(self, temp, raw0, raw_load=None, load_g=None):
        self.temp, self.raw0, self.raw_load, self.load_g = temp, raw0, raw_load, load_g


def temperature_compensation(x, points, recentre=False, max_order=2):
    """Solve Z(t) and S(t) so that, at every measured temperature, the zero reading equals the one at the reference and
    the load reading equals the load.  With only zero points S is left alone.  L is kept (re-expressed when T0 moves).
    -> dict(values {name: int}, t0, before/after drift [(temp, zero g, span error g)], order)"""
    pts = sorted(points, key=lambda p: p.temp)
    if len(pts) < 2:
        raise ModelError("need at least two temperature steps")
    uniq = sorted({p.temp // 400 for p in pts})          # 400 counts = 0.2 degC
    order = min(max_order, len(uniq) - 1)
    if order < 1:
        raise ModelError("the steps are all at the same temperature")
    t0_old = pm.g24(x, T0_AT)
    t0 = pts[len(pts) // 2].temp if recentre else t0_old
    units_per_g = pm.units(x)[0]
    k = pm.g24(x, SPAN_AT) + pm.g24(x, UCAL_AT)
    x0 = pm.g24(x, X0_AT)
    ref = min(pts, key=lambda p: abs(p.temp - t0_old))
    ts_old = [p.temp - t0_old for p in pts]
    Lt = lambda t: _poly(x, L_AT, t)
    Sold = lambda t: _poly(x, S_AT, t)

    def w_int(raw, s, z, L):
        xr = raw - x0
        return raw + z + (s + L * xr / 2 ** 24) * xr / 2 ** 24
    # target zero: the zero weight of the reference step under the present parameters
    c = w_int(ref.raw0, Sold(ref.temp - t0_old), _poly(x, Z_AT, ref.temp - t0_old), Lt(ref.temp - t0_old))
    zs, ss, with_load = [], [], all(p.raw_load is not None for p in pts)
    for p, t in zip(pts, ts_old):
        L = Lt(t)
        xa = p.raw0 - x0
        if with_load:
            xb = p.raw_load - x0
            target_w = p.load_g * units_per_g * 2 ** 23 / k
            s = (target_w - (p.raw_load - p.raw0) - L * (xb * xb - xa * xa) / 2 ** 48) / ((xb - xa) / 2 ** 24)
        else:
            s = Sold(t)
        z = c - p.raw0 - (s + L * xa / 2 ** 24) * xa / 2 ** 24
        zs.append(z); ss.append(s)
    # fit in the new reference (t relative to t0)
    tn = [p.temp - t0 for p in pts]
    zc, sc = fit_poly(tn, zs, order), fit_poly(tn, ss, order if with_load else order)
    if not with_load:
        sc = None
    # L re-expressed around the new T0 (exact for a quadratic)
    lvals = [Lt(t) for t in ts_old]
    lc = None
    if recentre:
        lc = fit_poly(tn, lvals, 2) if len(uniq) >= 3 else None
        if lc is None:
            raise ModelError("moving T0 needs three different temperatures")
    values = {"Z0": round(zc[0]), "Z1": round(zc[1]), "Z2": round(zc[2])}
    if sc is not None:
        values.update({"S0": round(sc[0]), "S1": round(sc[1]), "S2": round(sc[2])})
    elif recentre:
        scs = fit_poly(tn, [Sold(t) for t in ts_old], 2)
        values.update({"S0": round(scs[0]), "S1": round(scs[1]), "S2": round(scs[2])})
    if lc is not None:
        values.update({"L0": round(lc[0]), "L1": round(lc[1]), "L2": round(lc[2])})
    if recentre:
        values["T0"] = t0
    new = bytearray(x)
    for name, v in values.items():
        a = (pm.CELL.get(name) or pm.OTHER[name])[0]
        pm.s24(new, a, v)
    def drift(img):
        out = []
        for p in pts:
            z = weight_g(img, p.raw0, p.temp) - weight_g(img, ref.raw0, ref.temp)
            e = None
            if p.raw_load is not None:
                e = (weight_g(img, p.raw_load, p.temp) - weight_g(img, p.raw0, p.temp)) - p.load_g
            out.append((p.temp, z, e))
        return out
    return dict(values=values, t0=t0, order=order, with_load=with_load, image=new,
                before=drift(x), after=drift(new))
