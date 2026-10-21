"""
Service-manual adjustment procedures on a live balance, as a tab of pm_eeprom_gui: calibration resistor check,
linearity, calibration, temperature check and compensation.  The numbers come from pm_model; the readings
(raw A/D value, temperature count, stable flag) from the R, T and weight lines the balance streams (the R line
needs a cassette ROM with the raw line).  Results are applied to the image of the Live balance tab; writing it to
the balance is done there.
"""
import csv
import os
import re
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pm_eeprom as pm
import pm_model as mm
import pm_specs

E96 = [100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133, 137, 140, 143, 147, 150, 154, 158, 162, 165,
       169, 174, 178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232, 237, 243, 249, 255, 261, 267, 274, 280,
       287, 294, 301, 309, 316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412, 422, 432, 442, 453, 464, 475,
       487, 499, 511, 523, 536, 549, 562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732, 750, 768, 787, 806,
       825, 845, 866, 887, 909, 931, 953, 976]


def e96_near(ohm):
    """the E96 values just below and above ohm"""
    vals = sorted(v * 10 ** d / 100.0 for d in range(-2, 8) for v in E96)
    lo = max((v for v in vals if v <= ohm), default=vals[0])
    hi = min((v for v in vals if v >= ohm), default=vals[-1])
    return lo, hi


def fmt_ohm(v):
    return "%.4g k" % (v / 1000) if v >= 1000 else "%.4g " % v


class MiniPlot(tk.Canvas):
    """a few lines against temperature, no dependencies"""

    def __init__(self, master, **kw):
        super().__init__(master, height=170, background="white", highlightthickness=1, highlightbackground="#bbb", **kw)

    def plot(self, series, xlabel="", ylabel=""):
        self.delete("all")
        pts = [(x, y) for s in series.values() for x, y in zip(s[0], s[1]) if y is not None]
        if not pts:
            return
        w, h = int(self.winfo_width() or 400), int(self.cget("height"))
        x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
        y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
        if x1 == x0: x1 = x0 + 1
        if y1 == y0: y1 = y0 + 1
        pad = 36
        X = lambda v: pad + (v - x0) / (x1 - x0) * (w - pad - 10)
        Y = lambda v: h - 20 - (v - y0) / (y1 - y0) * (h - 34)
        self.create_line(pad, Y(0) if y0 < 0 < y1 else h - 20, w - 10, Y(0) if y0 < 0 < y1 else h - 20, fill="#ccc")
        self.create_text(pad - 4, 10, text="%.3g" % y1, anchor="e", font=("Menlo", 9))
        self.create_text(pad - 4, h - 20, text="%.3g" % y0, anchor="e", font=("Menlo", 9))
        self.create_text(pad, h - 8, text="%.3g" % x0, anchor="w", font=("Menlo", 9))
        self.create_text(w - 10, h - 8, text="%.3g %s" % (x1, xlabel), anchor="e", font=("Menlo", 9))
        self.create_text(w // 2, 8, text=ylabel, font=("Menlo", 9))
        for i, (name, (xs, ys, colour)) in enumerate(series.items()):
            line = [(X(a), Y(b)) for a, b in zip(xs, ys) if b is not None]
            for (xa, ya), (xb, yb) in zip(line, line[1:]):
                self.create_line(xa, ya, xb, yb, fill=colour)
            for xa, ya in line:
                self.create_oval(xa - 3, ya - 3, xa + 3, ya + 3, fill=colour, outline=colour)
            self.create_text(w - 12, 20 + 12 * i, text=name, fill=colour, anchor="e", font=("Menlo", 9))


class Procedures(ttk.Frame):
    def __init__(self, master, session, editor):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        self.s, self.ed = session, editor
        self.cap = {}                          # captured readings: key -> dict(raw, temp, ...)
        self._rearmed, self._ok_since = True, None
        self._build_strip()
        self._build_common()
        self._build_banner()
        self._build_applied()
        self.nb = ttk.Notebook(self)
        self.nb.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        for name, builder in (("1 Resistor", self._resistor), ("2 Linearity", self._linearity),
                              ("3 Calibration", self._calibration), ("4 Temperature", self._temperature)):
            f = ttk.Frame(self.nb, padding=8)
            self.nb.add(f, text=name)
            builder(f)
        self._collect_buttons()
        self._tick()


    # ------------------------------------------------------------ guided step banner
    BLUE, BLUE_DARK = "#E6F1FB", "#0C447C"

    def _build_banner(self):
        """native-looking panel with a coloured edge: ttk buttons keep their own background, so a filled colour box
        behind them shows as grey patches"""
        outer = ttk.Frame(self)
        outer.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        outer.columnconfigure(1, weight=1)
        tk.Frame(outer, background="#378ADD", width=6).grid(row=0, column=0, sticky="ns")
        f = ttk.Frame(outer, padding=(12, 8))
        f.grid(row=0, column=1, sticky="ew")
        f.columnconfigure(0, weight=1)
        self.b_title = ttk.Label(f, text="", font=("Helvetica", 17, "bold"), foreground=self.BLUE_DARK)
        self.b_title.grid(row=0, column=0, sticky="w")
        self.b_sub = ttk.Label(f, text="", foreground="#185FA5")
        self.b_sub.grid(row=1, column=0, sticky="w")
        self.b_state = ttk.Label(f, text="", font=("Helvetica", 12, "bold"))
        self.b_state.grid(row=2, column=0, sticky="w")
        r = ttk.Frame(f)
        r.grid(row=0, column=1, rowspan=3, padx=(12, 0))
        self.b_btn = ttk.Button(r, text="Capture", command=self.b_capture)
        self.b_btn.grid(row=0, column=0, padx=4)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="capture automatically\nwhen stable", variable=self.auto).grid(row=0, column=1, padx=8)
        ttk.Button(r, text="Start over", command=self.b_restart).grid(row=0, column=2)

    def _build_applied(self):
        self.applied = ttk.Frame(self)
        self.applied.columnconfigure(1, weight=1)
        tk.Frame(self.applied, background="#639922", width=6).grid(row=0, column=0, sticky="ns")
        self.applied_lbl = ttk.Label(self.applied, text="", foreground="#27500A", justify="left", wraplength=880)
        self.applied_lbl.grid(row=0, column=1, sticky="w", padx=10, pady=5)
        ttk.Button(self.applied, text="Undo", command=self.undo_applied).grid(row=0, column=2, padx=6)
        ttk.Button(self.applied, text="Dismiss", command=self.applied.grid_remove).grid(row=0, column=3)
        self.applied.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.applied.grid_remove()

    def undo_applied(self):
        self.ed.undo()
        self.applied.grid_remove()

    def _collect_buttons(self):
        self.cap_buttons, self.img_buttons = [], []

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Button):
                    t = str(c.cget("text"))
                    if t.startswith("Capture"):
                        self.cap_buttons.append(c)
                    elif t in ("Evaluate", "Compute", "Check drift (4.2.6)", "Fit compensation", "Default loads", "Build sequence"):
                        if t in ("Evaluate", "Compute", "Check drift (4.2.6)", "Fit compensation"):
                            self.img_buttons.append(c)
                walk(c)
        walk(self.nb)

    def prerequisite(self):
        """-> reason the procedures cannot run now, or None"""
        if self.s.link is None:
            return "Connect to the balance first (port in the top bar)"
        if not self.s.link.stream.fresh():
            return "No readings are arriving: wake the balance with the control bar (the raw line needs the --raw ROM)"
        if self.ed.data is None:
            return "Read the EEPROM first (Live balance tab, Read from balance)"
        return None

    def active(self):
        i = self.nb.index(self.nb.select())
        if i == 1:
            return "segregated" if self.lin_nb.index(self.lin_nb.select()) == 1 else "linearity"
        return ("resistor", None, "calibration", "temperature")[i]

    def step_info(self, key):
        """the step the user is on: dict(text, expected, i, n) or None when all are captured"""
        test = self.exp_test()
        if key in ("resistor", "calibration"):
            p = "r" if key == "resistor" else "c"
            if p + "_zero" not in self.cap:
                return dict(text="Empty pan: remove everything from the pan (it is tared first)", expected=0.0, i=1, n=2)
            if p + "_load" not in self.cap:
                return dict(text="Put the test weight on the pan" + (" (%g g)" % test if test else ""), expected=test, i=2, n=2)
            return None
        if key == "linearity":
            pts = self.l_points
            k = next((j for j, q in enumerate(pts) if q[1] is None), None)
            if k is None:
                return None
            m = pts[k][0]
            return dict(text="Empty pan" if m == 0 else "Put %g g on the pan" % m, expected=m, i=k + 1, n=len(pts))
        if key == "segregated":
            cfg = self.sg_cfg
            k = next((j for j, e in enumerate(cfg) if e[1] is None), None)
            if k is None:
                return None
            c = cfg[k][0]
            nominal = sum(self.sg_masses[j] for j in c)
            return dict(text="Empty pan" if not c else "Put weights %s on the pan" % self.sg_name(c), expected=nominal, i=k + 1, n=len(cfg))
        return None

    def exp_test(self):
        try:
            return float(self.test_g.get().strip().replace(",", "."))
        except ValueError:
            return None

    def gate(self, expected):
        """(ok, message): is the weight on the pan close to what the step needs?"""
        link = self.s.link
        w = link.stream.snapshot()["weight"] if link else None
        if expected is None or w is None:
            return True, ""
        tol = 1.0 if expected == 0 else 0.02 * abs(expected)
        if abs(w[0] - expected) <= tol:
            return True, ""
        return False, "The balance reads %.3f g, this step expects about %g g." % (w[0], expected)

    def step_start(self, key, force=False):
        if key in ("resistor", "calibration"):
            p = "r" if key == "resistor" else "c"
            if p + "_zero" not in self.cap:
                self.capture("capturing the empty pan", lambda r: self._got(p + "_zero", r), expected=0.0, force=force)
            else:
                self.capture("capturing the test weight", lambda r: self._got(p + "_load", r), expected=self.exp_test(), force=force)
        elif key == "linearity":
            self.l_capture(None, force)
        elif key == "segregated":
            self.sg_capture(None, force)

    def b_capture(self):
        self.step_start(self.active())

    def b_restart(self):
        key = self.active()
        if key in ("resistor", "calibration"):
            for k in [k for k in self.cap if k.startswith("r_" if key == "resistor" else "c_")]:
                del self.cap[k]
        elif key == "linearity":
            self.l_clear()
        elif key == "segregated":
            self.sg_clear()

    def _banner(self):
        key = self.active()
        why = self.prerequisite()
        info = None if why else self.step_info(key)
        busy = self.s.busy
        if why:
            self.b_title.configure(text=why); self.b_sub.configure(text=""); self.b_state.configure(text="")
        elif key == "temperature":
            self.b_title.configure(text="Capture the temperature steps below")
            self.b_sub.configure(text="zero now, then the load for the selected row; or import logs. Check or Fit when you have them.")
            self.b_state.configure(text="")
        elif info is None:
            self.b_title.configure(text="All steps captured")
            self.b_sub.configure(text="Press Evaluate / Compute below, or Start over.")
            self.b_state.configure(text="")
        else:
            self.b_title.configure(text=info["text"])
            self.b_sub.configure(text="step %d of %d" % (info["i"], info["n"]) + (
                "  ·  expect about %g g" % info["expected"] if info["expected"] else ""))
            sn = self.s.link.stream.snapshot()
            ok, msg = self.gate(info["expected"])
            w = sn["weight"]
            if not sn["stable"]:
                self.b_state.configure(text="reading %s g: moving, wait for it to settle" % ("%.3f" % w[0] if w else "-"), foreground="#854F0B")
            elif not ok and info["expected"] == 0 and w and w[0] < -1.0:
                self.b_state.configure(text="reading %.3f g: the tare is stale, Capture tares first" % w[0], foreground="#854F0B")
            elif not ok:
                self.b_state.configure(text=msg, foreground="#A32D2D")
            else:
                self.b_state.configure(text="reading %.3f g: stable, close to expected" % w[0], foreground="#3B6D11")
            self._auto(key, info, sn, ok)
        can_capture = not why and not busy and (info is not None)
        self.b_btn.state(["!disabled" if can_capture else "disabled"])
        for b in self.cap_buttons:
            b.state(["!disabled" if not why and not busy else "disabled"])
        for b in self.img_buttons:
            b.state(["!disabled" if self.ed.data is not None and not busy else "disabled"])
        link_ok = self.s.link is not None and self.s.link.stream.fresh() and not busy
        self.tare_btn.state(["!disabled" if link_ok else "disabled"])

    def _auto(self, key, info, sn, ok):
        if not self.auto.get() or self.s.busy:
            self._ok_since = None
            return
        now = time.time()
        w = sn["weight"]
        if (info["expected"] == 0 and sn["stable"] and not ok and w and w[0] < -1.0 and self._rearmed
                and time.time() - getattr(self, "_tare_try", 0) > 30):
            self._tare_try = time.time()                       # stale tare after removing a load: tare, then capture
            self.step_start(key)
            return
        if not (sn["stable"] and ok):
            self._rearmed, self._ok_since = True, None          # the weight moved or left the window: ready for the next capture
        elif self._rearmed:
            if self._ok_since is None:
                self._ok_since = now
            elif now - self._ok_since >= 1.5:
                self._ok_since = None
                self.step_start(key, force=True)

    # ------------------------------------------------------------ shared parts
    def _build_strip(self):
        f = ttk.Frame(self, padding=(6, 2))
        f.grid(row=0, column=0, sticky="ew")
        self.v_weight, self.v_raw, self.v_temp, self.v_state = (tk.StringVar(value="-") for _ in range(4))
        font = ("Menlo", 13)
        for i, (lbl, var, w) in enumerate((("weight", self.v_weight, 14), ("raw", self.v_raw, 11),
                                           ("temperature", self.v_temp, 26), ("", self.v_state, 30))):
            ttk.Label(f, text=lbl).grid(row=0, column=2 * i, sticky="e", padx=(8, 2))
            ttk.Label(f, textvariable=var, font=font, width=w).grid(row=0, column=2 * i + 1, sticky="w")
        self.tare_btn = ttk.Button(f, text="Tare", command=self.do_tare)
        self.tare_btn.grid(row=0, column=7, padx=4)
        ttk.Button(f, text="Stop capture", command=self._stop).grid(row=0, column=8, padx=8)
        self.progress = ttk.Progressbar(f, length=120, mode="determinate")
        self.progress.grid(row=0, column=9)

    def _build_common(self):
        f = ttk.Frame(self)
        f.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(f, text="Model").pack(side="left")
        self.model = ttk.Combobox(f, width=9, state="readonly", values=list(pm_specs.SPECS))
        self.model.pack(side="left", padx=4)
        self.model.bind("<<ComboboxSelected>>", lambda e: self._model_changed())
        self.model.set("PM4600")
        ttk.Label(f, text="Test weight [g]").pack(side="left", padx=(12, 2))
        self.test_g = ttk.Entry(f, width=9)
        self.test_g.pack(side="left")
        ttk.Label(f, text="samples per capture").pack(side="left", padx=(12, 2))
        self.n_samples = ttk.Spinbox(f, from_=10, to=200, width=5)
        self.n_samples.set(30)
        self.n_samples.pack(side="left")
        self.spec_lbl = ttk.Label(f, foreground="gray", wraplength=330)
        self.spec_lbl.pack(side="left", padx=12)
        self._model_changed()

    def spec(self):
        return pm_specs.SPECS[self.model.get()]

    def _model_changed(self):
        s = self.spec()
        self.test_g.delete(0, "end")
        self.test_g.insert(0, "%g" % s["cal_service"])
        self.spec_lbl.configure(text="linearity ±%g/%g mg, TC %g mg zero / %g mg span per 10 degC, R ref %s ohm" % (
            s["lin_up"], s["lin_down"], s["tc_zero"], s["tc_sens"], "/".join("%g" % r for r in s["r_ref"])))

    def img(self):
        if self.ed.data is None:
            messagebox.showwarning("No image", "Read the EEPROM from the balance first (Live balance tab) or open a file there.")
            return None
        return self.ed.data

    def link(self):
        return self.s.link if self.s.connected() else None

    def num(self, entry, what):
        try:
            return float(entry.get().strip().replace(",", "."))
        except ValueError:
            raise ValueError("%s: enter a number" % what)

    def _stop(self):
        self.s.cancel = True

    def _tick(self):
        link = self.s.link
        if link is not None:
            sn = link.stream.snapshot()
            w, t, r = sn["weight"], sn["temp"], sn["raw"]
            self.v_weight.set("%10.3f g" % w[0] if w else "-")
            self.v_raw.set("%d" % r[0] if r else "no R line")
            if t:
                x = self.ed.data
                rel = " (%+.2f degC vs T0)" % ((t[0] - pm.g24(x, 0x46)) / pm.T_COUNTS_PER_C) if x is not None else ""
                self.v_temp.set("%d%s" % (t[0], rel))
            self.v_state.set("stable" if sn["stable"] else "moving")
        else:
            for v in (self.v_weight, self.v_raw, self.v_temp, self.v_state):
                v.set("-")
        if self.s.busy:
            self.progress.configure(value=getattr(self, "_prog", 0))
        self._banner()
        self.after(300, self._tick)

    def do_tare(self, then=None):
        link = self.link()
        if link is None:
            return
        self.s.run(link.tare, lambda _r: (self.say("balance tared (reads zero)"), then() if then else None), "taring")

    def ensure_zero(self, then):
        """before an empty-pan step: a reading well off zero means the tare is stale. Negative (the pan is lighter than at
        the last tare) is tared silently; positive (something on the pan) asks first, so a load is never tared away."""
        link = self.s.link
        w = link.stream.snapshot()["weight"] if link else None
        if w is None or abs(w[0]) <= 1.0:
            then(); return
        if w[0] > 0 and not messagebox.askyesno(
                "Empty pan?", "The balance reads %+.3f g. If the pan is empty, press Yes to tare it and continue; if "
                              "something is on the pan, press No and remove it first." % w[0]):
            return
        self.do_tare(then)

    def capture(self, what, done, expected=None, force=False):
        """average of stable samples, in a worker thread; done(dict).  expected: the load this step needs, checked first"""
        link = self.link()
        if link is None:
            return
        ok, msg = self.gate(expected)
        if not ok and not force and expected == 0:
            self.ensure_zero(lambda: self.capture(what, done, expected, force=True))
            return
        if not ok and not force and not messagebox.askyesno("Check the load", msg + "\n\nCapture anyway?"):
            return
        inner = done

        def done(r):                                   # a finished capture waits for the weight to move before the next auto capture
            self._rearmed, self._ok_since = False, None
            inner(r)
        n = int(self.num(self.n_samples, "samples"))
        self.s.cancel = False
        self.progress.configure(maximum=n, value=0)
        prog = lambda i, k: setattr(self, "_prog", i)           # the Tk thread picks it up in _tick
        self.s.run(lambda: link.stream.capture(n, progress=prog, cancel=lambda: self.s.cancel),
                   lambda r: (self.progress.configure(value=0), done(r)), what)

    def say(self, text):
        self.ed.say(text)

    @staticmethod
    def text(parent, height=10):
        t = tk.Text(parent, height=height, wrap="word", font=("Menlo", 11))
        t.configure(state="disabled")
        return t

    @staticmethod
    def put(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def apply(self, values, note):
        x = self.img()
        if x is None:
            return
        before = {n: pm.g24(x, (pm.CELL.get(n) or pm.OTHER[n])[0]) for n in values}
        for name, v in values.items():
            a = (pm.CELL.get(name) or pm.OTHER[name])[0]
            old = pm.g24(x, a)
            pm.s24(x, a, int(v))
            self.say("%s: %d -> %d (%+d)" % (name, old, int(v), int(v) - old))
        self.ed.dirty = True
        self.ed._refresh()
        self.say(note + " - write the image to the balance to keep it")
        self.applied_lbl.configure(text="%s: %s. Not written yet: use Write to balance (top right) to keep it." % (
            note, ", ".join("%s %d -> %d" % (n, before[n], int(v)) for n, v in values.items())))
        self.applied.grid()

    def reading(self, key):
        c = self.cap.get(key)
        if c is None:
            raise ValueError("capture the %s first" % key)
        return c

    # ------------------------------------------------------------ 1 calibration resistor (manual 4.2.2, 4.2.3)
    def _resistor(self, f):
        f.columnconfigure(0, weight=1)
        ttk.Label(f, justify="left", foreground="gray", wraplength=1050,
                  text="Checks how far the analog gain is from the factory span (manual 4.2.2): the reading error at the test "
                       "weight with the factory span only, user calibration ignored. Above the limit (1.25 % PM, 5 % AM/CM) the "
                       "calibration resistor R42 has to change (4.2.3).\nEmpty pan, then the test weight; the balance must be "
                       "awake and show stable readings.").grid(row=0, column=0, columnspan=4, sticky="w")
        b = ttk.Frame(f); b.grid(row=1, column=0, sticky="w", pady=6)
        ttk.Button(b, text="Capture empty pan", command=lambda: self.capture("capturing the empty pan",
                   lambda r: self._got("r_zero", r), expected=0.0)).pack(side="left")
        ttk.Button(b, text="Capture test weight", command=lambda: self.capture("capturing the test weight",
                   lambda r: self._got("r_load", r), expected=self.exp_test())).pack(side="left", padx=6)
        ttk.Button(b, text="Evaluate", command=self.r_eval).pack(side="left")
        c = ttk.LabelFrame(f, text="R42", padding=6); c.grid(row=2, column=0, sticky="ew")
        self.r_mode = tk.StringVar(value="none")
        ttk.Radiobutton(c, text="R42 not fitted (error = reading - weight)", variable=self.r_mode, value="none").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(c, text="R42 fitted, value [ohm]:", variable=self.r_mode, value="fitted").grid(row=1, column=0, sticky="w")
        self.r_old = ttk.Entry(c, width=10); self.r_old.grid(row=1, column=1, padx=4)
        ttk.Label(c, text="Reference resistor [ohm]").grid(row=0, column=2, padx=(16, 2))
        self.r_ref = ttk.Entry(c, width=8); self.r_ref.grid(row=0, column=3)
        self.r_ref.insert(0, "530")
        self.r_out = self.text(f, 6); self.r_out.grid(row=3, column=0, sticky="nsew", pady=6)
        f.rowconfigure(3, weight=1)

    def _got(self, key, r):
        self.cap[key] = r
        self.say("%s: raw %.1f (std %.2f), temperature %.0f, %d samples" % (key, r["raw"], r["raw_std"], r["temp"], r["n"]))

    def r_eval(self):
        x = self.img()
        if x is None: return
        try:
            m = self.num(self.test_g, "test weight")
            z, l = self.reading("r_zero"), self.reading("r_load")
            err_pct, ind = mm.span_error_pct(x, l["raw"], l["temp"], z["raw"], z["temp"], m)
        except (ValueError, mm.ModelError) as e:
            messagebox.showerror("Resistor", str(e)); return
        limit = pm_specs.resistor_limit(self.model.get())
        out = ["factory-span reading at %g g: %.3f g   error %+.3f g = %+.3f %%   (limit %.2f %%)" % (m, ind, ind - m, err_pct, limit),
               "calibration absorbs this with the user calibration up to SPAN/32 = %.2f %%." % (100 / 32.0),
               "-> " + ("inside the limit: R42 is fine." if abs(err_pct) <= limit else "OUTSIDE the limit: change R42.")]
        try:
            rref = self.num(self.r_ref, "reference resistor")
            if self.r_mode.get() == "none":
                r = mm.r42_from_error(m, rref, ind - m)
                out.append("manual formula: R42 = %g g x %g ohm / %.3f g = %s ohm" % (m, rref, ind - m, fmt_ohm(r)))
            else:
                r = mm.r42_correction(m, rref, self.num(self.r_old, "R42"), ind - m)
                out.append("with R42 fitted: new R42 = %s ohm (assumes R42 lowers the reading by weight x Rref / R42)" % fmt_ohm(r))
            lo, hi = e96_near(r)
            out.append("nearest E96 values: %sohm / %sohm" % (fmt_ohm(lo), fmt_ohm(hi)))
        except (ValueError, mm.ModelError) as e:
            out.append("R42: " + str(e))
        self.put(self.r_out, "\n".join(out))

    # ------------------------------------------------------------ 2 linearity (manual 4.2.4)
    def _linearity(self, f):
        f.columnconfigure(0, weight=1); f.rowconfigure(0, weight=1)
        self.lin_nb = ttk.Notebook(f)
        self.lin_nb.grid(sticky="nsew")
        for name, builder in (("Known weights (manual)", self._linearity_known), ("Segregated weights", self._linearity_seg)):
            p = ttk.Frame(self.lin_nb, padding=6)
            self.lin_nb.add(p, text=name)
            builder(p)

    def _linearity_known(self, f):
        f.columnconfigure(0, weight=1); f.rowconfigure(2, weight=1)
        ttk.Label(f, justify="left", foreground="gray", wraplength=1050,
                  text="Zero, then loads from small to full (manual: half and full load; four or five points fit better). "
                       "The fit gives the L0 change that straightens the curve; the gain error it leaves is taken out by the "
                       "calibration (step 3). Press down the pan once at full load before measuring, as the manual says."
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f); b.grid(row=1, column=0, sticky="w", pady=6)
        ttk.Button(b, text="Default loads", command=self.l_defaults).pack(side="left")
        ttk.Button(b, text="Capture next", command=lambda: self.l_capture(None)).pack(side="left", padx=6)
        ttk.Button(b, text="Capture selected", command=lambda: self.l_capture(self.l_sel())).pack(side="left")
        ttk.Button(b, text="Add load...", command=self.l_add).pack(side="left", padx=6)
        ttk.Button(b, text="Clear readings", command=self.l_clear).pack(side="left")
        ttk.Button(b, text="Compute", command=self.l_compute).pack(side="left", padx=12)
        self.l_apply_btn = ttk.Button(b, text="Apply L0 to image", command=self.l_apply, state="disabled")
        self.l_apply_btn.pack(side="left")
        self.l_tree = ttk.Treeview(f, columns=("load", "raw", "temp", "before", "after", "tol"), show="headings", height=5)
        for c, w, t in (("load", 80, "load [g]"), ("raw", 100, "raw"), ("temp", 90, "temp count"),
                        ("before", 110, "error now [mg]"), ("after", 130, "after fit [mg]"), ("tol", 130, "tolerance [mg]")):
            self.l_tree.heading(c, text=t); self.l_tree.column(c, width=w, anchor="e")
        self.l_tree.grid(row=2, column=0, sticky="nsew")
        self.l_next = ttk.Label(f, font=("Menlo", 12)); self.l_next.grid(row=4, column=0, sticky="w")
        self.l_out = self.text(f, 3); self.l_out.grid(row=3, column=0, sticky="ew", pady=6)
        self.l_points = []                       # [load g, raw|None, temp|None]
        self.l_result = None
        self.l_defaults()

    def l_defaults(self):
        try:
            m = self.num(self.test_g, "test weight")
        except ValueError as e:
            messagebox.showerror("Linearity", str(e)); return
        self.l_points = [[m * k, None, None] for k in (0, 0.25, 0.5, 0.75, 1.0)]
        self.l_result = None; self.l_show()

    def l_show(self):
        self.l_tree.delete(*self.l_tree.get_children())
        res = self.l_result
        tol = self.spec()
        for i, (m, r, t) in enumerate(self.l_points):
            row = ["%g" % m, "" if r is None else "%.1f" % r, "" if t is None else "%.0f" % t, "", "", ""]
            if res and r is not None:
                bf, af = res["before"][i] * 1000, res["after"][i] * 1000
                row[3], row[4] = "%+.1f" % bf, "%+.1f" % af
                if 0 < i:
                    ok = abs(af) <= tol["lin_up"]
                    row[5] = "±%g  %s" % (tol["lin_up"], "ok" if ok else "OUT")
            self.l_tree.insert("", "end", iid=str(i), values=row)
        nxt = next((p[0] for p in self.l_points if p[1] is None), None)
        self.l_next.configure(text="" if nxt is None else
                              ("next: empty pan" if nxt == 0 else "next: put %g g on the pan, wait for 'stable', Capture next" % nxt))

    def l_sel(self):
        s = self.l_tree.selection()
        return int(s[0]) if s else None

    def l_add(self):
        from tkinter import simpledialog
        v = simpledialog.askfloat("Add load", "Load in grams:")
        if v is not None:
            self.l_points.append([v, None, None]); self.l_points.sort(key=lambda p: p[0]); self.l_show()

    def l_clear(self):
        for p in self.l_points: p[1] = p[2] = None
        self.l_result = None; self.l_show()

    def l_capture(self, i, force=False):
        if i is None:
            i = next((k for k, p in enumerate(self.l_points) if p[1] is None), None)
        if i is None:
            messagebox.showinfo("Linearity", "All points are captured; Compute, or select one to repeat."); return

        def done(r):
            self.l_points[i][1], self.l_points[i][2] = r["raw"], r["temp"]
            self.say("linearity %g g: raw %.1f (std %.2f)" % (self.l_points[i][0], r["raw"], r["raw_std"]))
            self.l_result = None; self.l_show()
        self.capture("capturing %g g" % self.l_points[i][0], done, expected=self.l_points[i][0], force=force)

    def l_compute(self):
        x = self.img()
        if x is None: return
        pts = [p for p in self.l_points if p[1] is not None]
        if not pts or pts[0][0] != 0:
            messagebox.showwarning("Linearity", "Capture the zero point (0 g) first."); return
        try:
            res = mm.linearity(x, [(p[0], p[1], p[2]) for p in pts])
        except mm.ModelError as e:
            messagebox.showerror("Linearity", str(e)); return
        res["rows"] = pts
        # spread captured values back over the full list for display
        full_b, full_a, k = [], [], 0
        for p in self.l_points:
            if p[1] is None: full_b.append(0.0); full_a.append(0.0)
            else: full_b.append(res["before"][k]); full_a.append(res["after"][k]); k += 1
        res["before"], res["after"], res["rows"] = full_b, full_a, self.l_points
        self.l_result = res; self.l_show()
        worst = max(abs(a) for a in full_a[1:]) * 1000
        self.put(self.l_out, "ΔL0 = %+d (L0 %d -> %d). Largest remaining error %.1f mg (tolerance %g mg), after recalibration.\n"
                             "Gain error removed by the fit: %+.3f %% (taken out by step 3)." % (
                 res["delta_l0"], pm.g24(x, 0x5E), res["new_l0"], worst, self.spec()["lin_up"], res["gain"] * 100))
        self.l_apply_btn.configure(state="normal")

    def l_apply(self):
        if self.l_result:
            self.apply({"L0": self.l_result["new_l0"]}, "linearity applied")
            self.l_apply_btn.configure(state="disabled")


    # ---- linearity with segregated weights: masses need not be known, only constant
    def _linearity_seg(self, f):
        f.columnconfigure(0, weight=1); f.rowconfigure(3, weight=1)
        ttk.Label(f, justify="left", foreground="gray", wraplength=1050,
                  text="For when no weight is accurate enough to test linearity in the milligram region. Use several weights, "
                       "each alone and then in growing combinations. Their masses are not needed (only that they stay the same): "
                       "the fit solves the masses and the L0 change together, so the weights' own errors and the gain error "
                       "drop out. More and more varied combinations make a more reliable fit; each weight must appear in at "
                       "least two configurations.").grid(row=0, column=0, sticky="w")
        top = ttk.Frame(f); top.grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(top, text="Weights [g], comma separated:").pack(side="left")
        self.sg_weights = ttk.Entry(top, width=34); self.sg_weights.pack(side="left", padx=4)
        self.sg_weights.insert(0, "1000, 1000, 1000, 1000")
        ttk.Button(top, text="Build sequence", command=self.sg_build).pack(side="left")
        ttk.Button(top, text="Add combination...", command=self.sg_add).pack(side="left", padx=6)
        b = ttk.Frame(f); b.grid(row=2, column=0, sticky="w", pady=4)
        ttk.Button(b, text="Capture next", command=lambda: self.sg_capture(None)).pack(side="left")
        ttk.Button(b, text="Capture selected", command=lambda: self.sg_capture(self.sg_sel())).pack(side="left", padx=6)
        ttk.Button(b, text="Clear readings", command=self.sg_clear).pack(side="left")
        ttk.Button(b, text="Compute", command=self.sg_compute).pack(side="left", padx=12)
        self.sg_apply_btn = ttk.Button(b, text="Apply L0 to image", command=self.sg_apply, state="disabled")
        self.sg_apply_btn.pack(side="left")
        self.sg_tree = ttk.Treeview(f, columns=("step", "nominal", "raw", "temp", "before", "after"), show="headings", height=5)
        for c, w, t in (("step", 140, "on the pan"), ("nominal", 80, "nominal [g]"), ("raw", 100, "raw"),
                        ("temp", 90, "temp count"), ("before", 130, "residual now [mg]"), ("after", 130, "after fit [mg]")):
            self.sg_tree.heading(c, text=t); self.sg_tree.column(c, width=w, anchor="e" if c != "step" else "w")
        self.sg_tree.grid(row=3, column=0, sticky="nsew")
        self.sg_next = ttk.Label(f, font=("Menlo", 12)); self.sg_next.grid(row=4, column=0, sticky="w")
        self.sg_out = self.text(f, 3); self.sg_out.grid(row=5, column=0, sticky="ew", pady=4)
        self.sg_masses = []                      # nominal grams, names A, B, C...
        self.sg_cfg = []                         # [frozenset of indices, raw|None, temp|None]; the first entry is the empty pan
        self.sg_result = None
        self.sg_build(quiet=True)

    def sg_name(self, c):
        return "empty pan" if not c else "+".join(chr(65 + j) for j in sorted(c))

    def sg_build(self, quiet=False):
        try:
            self.sg_masses = [float(v) for v in self.sg_weights.get().replace(";", ",").split(",") if v.strip()]
        except ValueError:
            messagebox.showerror("Weights", "Enter the nominal weights as numbers, e.g. 1000, 500, 200, 200."); return
        n = len(self.sg_masses)
        if not 2 <= n <= 8:
            messagebox.showerror("Weights", "Use between 2 and 8 weights."); return
        cfg = [frozenset()] + [frozenset({j}) for j in range(n)]                       # each alone
        cfg += [frozenset(range(k)) for k in range(2, n + 1)]                           # growing sums A+B, A+B+C ...
        cfg += [frozenset(range(1, n))] if n > 2 else []                               # all but A
        seen, out = set(), []
        for c in cfg:
            if c not in seen:
                seen.add(c); out.append([c, None, None])
        self.sg_cfg = out
        self.sg_result = None
        self.sg_show()

    def sg_add(self):
        from tkinter import simpledialog
        txt = simpledialog.askstring("Add combination", "Letters of the weights on the pan, e.g. BD (A is the first weight):")
        if not txt:
            return
        idx = {ord(ch.upper()) - 65 for ch in txt if ch.strip()}
        if not idx or max(idx) >= len(self.sg_masses) or min(idx) < 0:
            messagebox.showerror("Combination", "Use the letters A..%s." % chr(64 + len(self.sg_masses))); return
        c = frozenset(idx)
        if any(e[0] == c for e in self.sg_cfg):
            return
        self.sg_cfg.append([c, None, None]); self.sg_result = None; self.sg_show()

    def sg_show(self):
        self.sg_tree.delete(*self.sg_tree.get_children())
        res = self.sg_result
        for i, (c, r, t) in enumerate(self.sg_cfg):
            row = [self.sg_name(c), "%g" % sum(self.sg_masses[j] for j in c), "" if r is None else "%.1f" % r,
                   "" if t is None else "%.0f" % t, "", ""]
            if res and c and r is not None and i in res["index"]:
                k = res["index"][i]
                row[4], row[5] = "%+.1f" % (res["before"][k] * 1000), "%+.1f" % (res["after"][k] * 1000)
            self.sg_tree.insert("", "end", iid=str(i), values=row)
        nxt = next((e[0] for e in self.sg_cfg if e[1] is None), None)
        self.sg_next.configure(text="" if nxt is None else ("next: empty pan" if not nxt else
                               "next: put %s on the pan, wait for 'stable', Capture next" % self.sg_name(nxt)))

    def sg_sel(self):
        s = self.sg_tree.selection()
        return int(s[0]) if s else None

    def sg_clear(self):
        for e in self.sg_cfg: e[1] = e[2] = None
        self.sg_result = None; self.sg_show()

    def sg_capture(self, i, force=False):
        if i is None:
            i = next((k for k, e in enumerate(self.sg_cfg) if e[1] is None), None)
        if i is None:
            messagebox.showinfo("Linearity", "All steps are captured; Compute, or select one to repeat."); return

        def done(r):
            self.sg_cfg[i][1], self.sg_cfg[i][2] = r["raw"], r["temp"]
            self.say("segregated %s: raw %.1f (std %.2f)" % (self.sg_name(self.sg_cfg[i][0]), r["raw"], r["raw_std"]))
            self.sg_result = None; self.sg_show()
        self.capture("capturing %s" % self.sg_name(self.sg_cfg[i][0]), done,
                     expected=sum(self.sg_masses[j] for j in self.sg_cfg[i][0]), force=force)

    def sg_compute(self):
        x = self.img()
        if x is None: return
        if self.sg_cfg[0][1] is None:
            messagebox.showwarning("Linearity", "Capture the empty pan first."); return
        got = [(i, e) for i, e in enumerate(self.sg_cfg) if e[0] and e[1] is not None]
        try:
            res = mm.linearity_combinations(x, (self.sg_cfg[0][1], self.sg_cfg[0][2]),
                                            [(set(e[0]), e[1], e[2]) for _, e in got], len(self.sg_masses))
        except mm.ModelError as e:
            messagebox.showerror("Linearity", str(e)); return
        res["index"] = {i: k for k, (i, _) in enumerate(got)}
        self.sg_result = res; self.sg_show()
        worst = max(abs(a) for a in res["after"]) * 1000
        masses = ", ".join("%s %.3f g (nominal %g)" % (chr(65 + j), m, self.sg_masses[j]) for j, m in enumerate(res["masses"]))
        self.put(self.sg_out, "ΔL0 = %+d (L0 %d -> %d). Largest residual after the fit %.1f mg (tolerance %g mg).\n"
                              "Masses as the balance sees them now: %s\n"
                              "(the common factor is the gain error that step 3 takes out; differences between weights are real.)" % (
                 res["delta_l0"], pm.g24(x, 0x5E), res["new_l0"], worst, self.spec()["lin_up"], masses))
        self.sg_apply_btn.configure(state="normal")

    def sg_apply(self):
        if self.sg_result:
            self.apply({"L0": self.sg_result["new_l0"]}, "linearity (segregated weights) applied")
            self.sg_apply_btn.configure(state="disabled")

    # ------------------------------------------------------------ 3 calibration (manual 4.2.5)
    def _calibration(self, f):
        f.columnconfigure(0, weight=1)
        ttk.Label(f, justify="left", foreground="gray", wraplength=1050,
                  text="Sets the user calibration (UCAL) so that the tared reading at the test weight equals the weight. "
                       "Does what the balance's own calibration does, without starting it (the cassette's CA command is not used). "
                       "Do the linearity first; check ¼ and ¾ load afterwards."
                  ).grid(row=0, column=0, sticky="w")
        b = ttk.Frame(f); b.grid(row=1, column=0, sticky="w", pady=6)
        ttk.Button(b, text="Capture empty pan", command=lambda: self.capture("capturing the empty pan",
                   lambda r: self._got("c_zero", r), expected=0.0)).pack(side="left")
        ttk.Button(b, text="Capture test weight", command=lambda: self.capture("capturing the test weight",
                   lambda r: self._got("c_load", r), expected=self.exp_test())).pack(side="left", padx=6)
        ttk.Button(b, text="Compute", command=self.c_compute).pack(side="left")
        self.c_btn = ttk.Button(b, text="Apply UCAL to image", command=self.c_apply, state="disabled")
        self.c_btn.pack(side="left", padx=6)
        self.c_out = self.text(f, 5); self.c_out.grid(row=2, column=0, sticky="nsew")
        f.rowconfigure(2, weight=1)
        self.c_new = None

    def c_compute(self):
        x = self.img()
        if x is None: return
        try:
            m = self.num(self.test_g, "test weight")
            z, l = self.reading("c_zero"), self.reading("c_load")
            ucal, ind, lim = mm.calibration(x, l["raw"], l["temp"], z["raw"], z["temp"], m)
        except (ValueError, mm.ModelError) as e:
            messagebox.showerror("Calibration", str(e)); return
        old = pm.g24(x, 0x32)
        txt = ["reading now %.3f g at %g g (%+.3f g, %+.4f %%)" % (ind, m, ind - m, (ind - m) / m * 100),
               "UCAL %d -> %d (limit ±%d)" % (old, ucal, lim)]
        ok = abs(ucal) <= lim
        txt.append("-> " + ("fits inside the user-calibration range." if ok else
                            "OUT OF RANGE: the factory span is too far off, fix the calibration resistor (step 1) first."))
        self.put(self.c_out, "\n".join(txt))
        self.c_new = ucal if ok else None
        self.c_btn.configure(state="normal" if ok else "disabled")

    def c_apply(self):
        if self.c_new is not None:
            self.apply({"UCAL": self.c_new}, "calibration applied")
            self.c_btn.configure(state="disabled")

    # ------------------------------------------------------------ 4 temperature (manual 4.2.6, 4.2.7)
    def _temperature(self, f):
        f.columnconfigure(0, weight=1); f.rowconfigure(3, weight=1)
        ttk.Label(f, justify="left", foreground="gray", wraplength=1050,
                  text="Each row is one temperature step: the zero reading, and the reading under the test weight (leave the load "
                       "out to fit the zero drift alone). Take the steps as the balance warms up (manual: switch-on, +2 h, +4 h) or "
                       "import overnight logs. Check = the manual's drift test; Fit = compensation for the steps in the table."
                  ).grid(row=0, column=0, sticky="w")
        b = ttk.Frame(f); b.grid(row=1, column=0, sticky="w", pady=6)
        ttk.Button(b, text="Capture zero now", command=self.t_zero).pack(side="left")
        ttk.Button(b, text="Capture load for selected", command=self.t_load).pack(side="left", padx=6)
        ttk.Button(b, text="Import logs...", command=self.t_import).pack(side="left")
        ttk.Button(b, text="Remove", command=self.t_remove).pack(side="left", padx=6)
        ttk.Button(b, text="Clear", command=self.t_clear).pack(side="left")
        c = ttk.Frame(f); c.grid(row=2, column=0, sticky="w")
        ttk.Button(c, text="Check drift (4.2.6)", command=self.t_check).pack(side="left")
        ttk.Button(c, text="Fit compensation", command=self.t_fit).pack(side="left", padx=6)
        self.t_recentre = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="move T0 to the middle step (as the manual)", variable=self.t_recentre).pack(side="left")
        self.t_apply_btn = ttk.Button(c, text="Apply to image", command=self.t_apply, state="disabled")
        self.t_apply_btn.pack(side="left", padx=6)
        self.t_tree = ttk.Treeview(f, columns=("temp", "dc", "zero", "load", "g"), show="headings", height=5)
        for col, w, t in (("temp", 90, "temp count"), ("dc", 90, "vs T0 [degC]"), ("zero", 100, "zero raw"),
                          ("load", 100, "load raw"), ("g", 70, "load [g]")):
            self.t_tree.heading(col, text=t); self.t_tree.column(col, width=w, anchor="e")
        self.t_tree.grid(row=3, column=0, sticky="nsew", pady=4)
        self.t_out = self.text(f, 5); self.t_out.grid(row=4, column=0, sticky="ew")
        self.t_plot = MiniPlot(f); self.t_plot.grid(row=5, column=0, sticky="ew", pady=4)
        self.t_points = []                        # TcPoint
        self.t_result = None

    def t_show(self):
        self.t_tree.delete(*self.t_tree.get_children())
        x = self.ed.data
        t0 = pm.g24(x, 0x46) if x is not None else 0
        for i, p in enumerate(self.t_points):
            self.t_tree.insert("", "end", iid=str(i), values=(
                "%.0f" % p.temp, "%+.2f" % ((p.temp - t0) / pm.T_COUNTS_PER_C), "%.1f" % p.raw0,
                "" if p.raw_load is None else "%.1f" % p.raw_load, "" if p.load_g is None else "%g" % p.load_g))

    def t_zero(self):
        def done(r):
            self.t_points.append(mm.TcPoint(r["temp"], r["raw"]))
            self.t_points.sort(key=lambda p: p.temp); self.t_show()
            self.say("temperature step %.0f: zero raw %.1f" % (r["temp"], r["raw"]))
        self.capture("capturing the zero", done, expected=0.0)

    def t_load(self):
        s = self.t_tree.selection()
        if not s:
            messagebox.showinfo("Temperature", "Select the step the load belongs to (take it at the same temperature)."); return
        i = int(s[0])
        try:
            m = self.num(self.test_g, "test weight")
        except ValueError as e:
            messagebox.showerror("Temperature", str(e)); return

        def done(r):
            p = self.t_points[i]
            if abs(r["temp"] - p.temp) > 1000:
                messagebox.showwarning("Temperature", "The temperature moved %.1f degC since the zero of this step; the load "
                                       "reading is stored with the zero's temperature, take zero and load close together." %
                                       ((r["temp"] - p.temp) / pm.T_COUNTS_PER_C))
            p.raw_load, p.load_g = r["raw"], m
            self.t_show(); self.say("step %.0f: load %g g raw %.1f" % (p.temp, m, r["raw"]))
        self.capture("capturing the load", done, expected=m)

    def t_remove(self):
        for s in sorted((int(i) for i in self.t_tree.selection()), reverse=True):
            del self.t_points[s]
        self.t_show()

    def t_clear(self):
        self.t_points = []; self.t_result = None; self.t_show()
        self.put(self.t_out, ""); self.t_plot.delete("all")

    def t_import(self):
        zero = filedialog.askopenfilename(title="Log of the EMPTY pan (pm_logger CSV, with R lines)",
                                          filetypes=[("CSV", "*.csv"), ("All", "*")])
        if not zero:
            return
        load = filedialog.askopenfilename(title="Optional: log with the test weight on (Cancel to skip)",
                                          filetypes=[("CSV", "*.csv"), ("All", "*")])
        try:
            z = read_log(zero)
            ld = read_log(load) if load else None
            self.t_points = bin_points(z, ld, self.num(self.test_g, "test weight") if ld else None)
        except (OSError, ValueError, mm.ModelError) as e:
            messagebox.showerror("Import", str(e)); return
        self.t_result = None; self.t_show()
        self.say("imported %d temperature steps from the logs" % len(self.t_points))

    def t_check(self):
        x = self.img()
        if x is None: return
        pts = [p for p in self.t_points if p.raw_load is not None]
        try:
            if len(self.t_points) < 2:
                raise ValueError("need two steps (switch-on and after warming)")
            a, b = self.t_points[0], self.t_points[-1]
            wz = lambda p: mm.weight_g(x, p.raw0, p.temp)
            z, s, dt = mm.drift_per_c(a.temp, b.temp, wz(a), wz(b), 0, 0)
            txt = ["temperature change %.2f degC (%.0f -> %.0f counts)" % (dt, a.temp, b.temp),
                   "zero drift  %+.1f mg/degC   tolerance %.1f mg/degC (%g mg per 10 degC)" % (
                       z * 1000, self.spec()["tc_zero"] / 10, self.spec()["tc_zero"])]
            ok = abs(z * 1000) <= self.spec()["tc_zero"] / 10
            if a.raw_load is not None and b.raw_load is not None:
                f = lambda p: mm.weight_g(x, p.raw_load, p.temp) - mm.weight_g(x, p.raw0, p.temp)
                _, s, _ = mm.drift_per_c(a.temp, b.temp, 0, 0, f(a), f(b))
                txt.append("sensitivity drift  %+.1f mg/degC   tolerance %.1f mg/degC" % (s * 1000, self.spec()["tc_sens"] / 10))
                ok = ok and abs(s * 1000) <= self.spec()["tc_sens"] / 10
            txt.append("-> " + ("within the tolerances: no compensation needed." if ok else
                                "above the tolerance (the manual compensates from 1.5 to 2 times): fit the compensation."))
            self.put(self.t_out, "\n".join(txt))
        except (ValueError, mm.ModelError) as e:
            messagebox.showerror("Temperature", str(e))

    def t_fit(self):
        x = self.img()
        if x is None: return
        try:
            res = mm.temperature_compensation(x, self.t_points, self.t_recentre.get())
        except mm.ModelError as e:
            messagebox.showerror("Temperature", str(e)); return
        self.t_result = res
        names = " ".join("%s=%d" % kv for kv in res["values"].items())
        zb = max(abs(z) for _, z, _ in res["before"]) * 1000
        za = max(abs(z) for _, z, _ in res["after"]) * 1000
        txt = ["fit of order %d over %d steps (%s)" % (res["order"], len(self.t_points), "zero and span" if res["with_load"] else "zero only"),
               names, "largest zero change over the steps: %.1f mg now -> %.1f mg after" % (zb, za)]
        if res["with_load"]:
            eb = max(abs(e) for _, _, e in res["before"]) * 1000
            ea = max(abs(e) for _, _, e in res["after"]) * 1000
            txt.append("largest span error at the test weight: %.1f mg now -> %.1f mg after" % (eb, ea))
        if res["order"] < 2:
            txt.append("only two distinct temperatures: linear fit; take a third step for the quadratic term.")
        span = (max(p.temp for p in self.t_points) - min(p.temp for p in self.t_points)) / pm.T_COUNTS_PER_C
        if span < 3:
            txt.append("WARNING: the steps cover only %.1f degC: the fit is not reliable outside that range." % span)
        self.put(self.t_out, "\n".join(txt))
        T = [p.temp for p in self.t_points]
        series = {"zero now": (T, [z * 1000 for _, z, _ in res["before"]], "#c0392b"),
                  "zero after": (T, [z * 1000 for _, z, _ in res["after"]], "#27ae60")}
        self.t_plot.plot(series, "count", "zero change [mg]")
        self.t_apply_btn.configure(state="normal")

    def t_apply(self):
        if self.t_result:
            self.apply(self.t_result["values"], "temperature compensation applied")
            self.t_apply_btn.configure(state="disabled")


# ---------------------------------------------------------------- log import
def read_log(path):
    """pm_logger CSV -> [(raw, temperature count)] from the R lines (temperature from the long R lines and the T lines)"""
    out, temp = [], None
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            line = (row.get("raw") or "").strip()
            m = re.match(r"^T (\d+)$", line)
            if m:
                temp = int(m.group(1)); continue
            m = re.match(r"^R ([0-9A-F]{8}) ([0-9A-F]{6})(?: ([0-9A-F]{6}) ([0-9A-F]{6}) ([0-9A-F]{2}))?$", line)
            if m:
                if m.group(4):
                    temp = int(m.group(4), 16)
                raw = int(m.group(2), 16)
                if raw & 0x800000:
                    raw -= 1 << 24
                if temp is not None:
                    out.append((raw, temp))
    if len(out) < 50:
        raise ValueError("%s: fewer than 50 R lines with a temperature (log with the raw-line ROM and the logger of this tool)" % os.path.basename(path))
    return out


def bin_points(zero, load, load_g, width=500, min_samples=20):
    """average the samples in temperature bins (500 counts = 0.25 degC); with a load log, pair its bins with the zero
    reading interpolated at the same temperature (quadratic fit of the zero log)"""
    def bins(data):
        d = {}
        for raw, t in data:
            d.setdefault(t // width, []).append((raw, t))
        return {k: (sum(r for r, _ in v) / len(v), sum(t for _, t in v) / len(v)) for k, v in d.items() if len(v) >= min_samples}
    zb = bins(zero)
    if len(zb) < 2:
        raise ValueError("the empty-pan log covers fewer than two temperature bins with %d samples" % min_samples)
    pts = [mm.TcPoint(t, r) for r, t in (zb[k] for k in sorted(zb))]
    if load is None:
        return pts
    lb = bins(load)
    if not lb:
        raise ValueError("the load log has no temperature bin with %d samples" % min_samples)
    ts = [p.temp for p in pts]; rs = [p.raw0 for p in pts]
    order = min(2, len(pts) - 1)
    c = mm.fit_poly([t - ts[0] for t in ts], rs, order)
    zero_at = lambda t: c[0] + c[1] * (t - ts[0]) / 65536.0 + c[2] * (t - ts[0]) ** 2 / 4294967296.0
    return [mm.TcPoint(t, zero_at(t), r, load_g) for r, t in (lb[k] for k in sorted(lb))]
