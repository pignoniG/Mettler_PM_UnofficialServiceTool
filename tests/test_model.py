"""Tests of pm_model: synthetic balances whose physics is known, adjusted with the manual's procedures."""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
import pm_eeprom as pm, pm_model as mm, pm_specs

EEP = os.environ.get('PM_EEPROM')


def blank_image():
    """a parameter image with neutral corrections, built from a real dump when given, else from the structure alone"""
    if EEP and os.path.exists(EEP):
        x = bytearray(pm.load(EEP)[0])
    else:
        raise unittest.SkipTest('set PM_EEPROM to a PM EEPROM dump')
    for n in ('Z0', 'Z1', 'Z2', 'S0', 'S1', 'S2', 'L0', 'L1', 'L2'):
        pm.s24(x, pm.CELL[n][0], 0)
    return x


class Phys:
    """raw value of a simulated cell: offset and gain drift with temperature, quadratic load term"""
    def __init__(self, x, zero_per_c=-3.0, gain_ppm=60.0, bow=2e-12):
        self.u = pm.units(x)[0]
        self.k = pm.g24(x, 0x2E) + pm.g24(x, 0x32)
        self.rpg = self.u * 2 ** 23 / self.k              # raw counts per gram that make the display right
        self.t0 = pm.g24(x, 0x46)
        self.r0 = 1500000
        self.zc, self.gp, self.bow = zero_per_c, gain_ppm, bow
        self.rpg *= 1.004                                 # a span error the calibration has to take out

    def raw(self, m, t):
        dt = (t - self.t0) / 2000.0
        return round(self.r0 + self.zc * 2000 * dt + m * self.rpg * (1 + self.gp * 1e-6 * dt) * 1.0
                     + self.bow * (m * self.rpg) ** 2)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.x = blank_image()
        self.p = Phys(self.x)
        self.t = self.p.t0

    def test_calibration_takes_out_span_error(self):
        r0, r1 = self.p.raw(0, self.t), self.p.raw(4000, self.t)
        ucal, ind, lim = mm.calibration(self.x, r1, self.t, r0, self.t, 4000)
        y = bytearray(self.x); pm.s24(y, 0x32, ucal)
        self.assertAlmostEqual(mm.indicated_g(y, r1, self.t, r0, self.t), 4000, delta=0.005)

    def test_linearity_fit_flattens_the_bow(self):
        loads = [0, 1000, 2000, 3000, 4000]
        pts = [(m, self.p.raw(m, self.t), self.t) for m in loads]
        res = mm.linearity(self.x, pts)
        self.assertGreater(max(abs(e) for e in res['before']), 0.05)
        self.assertLess(max(abs(e) for e in res['after']), 0.01)
        # applied, then recalibrated, the readings at 1/4, 1/2, 3/4 load are inside a few mg
        y = bytearray(self.x); pm.s24(y, 0x5E, res['new_l0'])
        r0, r4 = self.p.raw(0, self.t), self.p.raw(4000, self.t)
        ucal = mm.calibration(y, r4, self.t, r0, self.t, 4000)[0]; pm.s24(y, 0x32, ucal)
        for m in (1000, 2000, 3000):
            self.assertAlmostEqual(mm.indicated_g(y, self.p.raw(m, self.t), self.t, r0, self.t), m, delta=0.01)

    def test_linearity_with_unknown_weights(self):
        masses = [1000.37, 999.88, 1000.12, 1000.51]
        sets = [{0}, {1}, {2}, {3}, {0, 1}, {0, 1, 2}, {0, 1, 2, 3}, {1, 3}]
        r0 = self.p.raw(0, self.t)
        cfg = [(c, self.p.raw(sum(masses[j] for j in c), self.t), self.t) for c in sets]
        res = mm.linearity_combinations(self.x, (r0, self.t), cfg, 4)
        self.assertGreater(max(abs(e) for e in res['before']), 0.005)
        self.assertLess(max(abs(e) for e in res['after']), 0.003)
        # the fitted masses are the true ones times the gain error of the span, the same factor for all
        f = [m / t for m, t in zip(res['masses'], masses)]
        self.assertLess(max(f) - min(f), 2e-5)
        # same cure as the known-weights method
        known = mm.linearity(self.x, [(0, r0, self.t)] + [(sum(masses[j] for j in c), self.p.raw(sum(masses[j] for j in c), self.t), self.t)
                                                            for c in sets[:1] + sets[4:7]])
        self.assertAlmostEqual(res['delta_l0'], known['delta_l0'], delta=abs(known['delta_l0']) * 0.1 + 20)

    def test_linearity_combinations_need_enough_configurations(self):
        with self.assertRaises(mm.ModelError):
            mm.linearity_combinations(self.x, (1, 1), [({0}, 1, 1), ({1}, 1, 1), ({0, 1}, 1, 1)], 2 + 1)

    def test_temperature_fit_removes_zero_and_span_drift(self):
        temps = [self.t + d * 2000 for d in (-6, -3, 0, 3, 6)]
        pts = [mm.TcPoint(t, self.p.raw(0, t), self.p.raw(4000, t), 4000) for t in temps]
        res = mm.temperature_compensation(self.x, pts)
        before = max(abs(z) for _, z, _ in res['before']); after = max(abs(z) for _, z, _ in res['after'])
        self.assertGreater(before, 0.1); self.assertLess(after, 0.01)
        self.assertLess(max(abs(e) for _, _, e in res['after']), 0.02)
        self.assertGreater(max(abs(e) for _, _, e in res['before']), 0.5)

    def test_temperature_fit_zero_only_leaves_span(self):
        temps = [self.t + d * 2000 for d in (-4, 0, 4)]
        pts = [mm.TcPoint(t, self.p.raw(0, t)) for t in temps]
        res = mm.temperature_compensation(self.x, pts)
        self.assertNotIn('S1', res['values'])
        self.assertLess(max(abs(z) for _, z, _ in res['after']), 0.01)

    def test_recentre_moves_t0_and_keeps_the_result(self):
        temps = [self.t + d * 2000 for d in (2, 5, 8)]
        pts = [mm.TcPoint(t, self.p.raw(0, t), self.p.raw(4000, t), 4000) for t in temps]
        res = mm.temperature_compensation(self.x, pts, recentre=True)
        self.assertEqual(res['values']['T0'], temps[1])
        self.assertLess(max(abs(z) for _, z, _ in res['after']), 0.01)
        self.assertLess(max(abs(e) for _, _, e in res['after']), 0.02)

    def test_resistor_formula_of_the_manual(self):
        self.assertAlmostEqual(mm.r42_from_error(4000, 530, 103.78), 20428, delta=2)
        r = mm.r42_correction(4000, 530, 20428, 0.0)
        self.assertAlmostEqual(r, 20428, delta=0.01)
        self.assertLess(mm.r42_correction(4000, 530, 20428, 2.0), 20428)      # reading too high: more correction

    def test_drift_per_c_example_of_the_manual(self):
        z, s, dt = mm.drift_per_c(297904, 312108, 0.0, 0.05, 3999.96, 4000.03)
        self.assertAlmostEqual(dt, 7.1, places=1)
        self.assertAlmostEqual(z, 0.0070, places=3); self.assertAlmostEqual(s, 0.0099, places=3)

    def test_specs(self):
        self.assertEqual(pm_specs.SPECS['PM4600']['tc_sens'], 180)
        self.assertEqual(pm_specs.guess_model(4000), 'PM4000')


if __name__ == '__main__':
    unittest.main()
