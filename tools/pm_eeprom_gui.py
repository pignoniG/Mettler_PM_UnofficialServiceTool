#!/usr/bin/env python3
"""
Graphical front end for pm_eeprom.py - Mettler AM/PM series parameter EEPROM editor.

Standard library only (tkinter). Run with:   python3 pm_eeprom_gui.py [dump.BIN]
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_eeprom as pm


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.rowconfigure(1, weight=1)

        self.path = None            # file the data came from
        self.data = None            # bytearray in 8051 order
        self.original = None        # for Revert
        self.swapped = True         # byte order of the file on disk
        self.dirty = False

        self._build_toolbar()
        self._build_info()
        self._build_actions()
        self._build_log()
        self._refresh()

    # ------------------------------------------------------------ layout
    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(bar, text="Open dump...", command=self.do_open).pack(side="left")
        ttk.Button(bar, text="Save as...", command=self.do_save).pack(side="left", padx=4)
        ttk.Button(bar, text="Revert", command=self.do_revert).pack(side="left")
        self.file_lbl = ttk.Label(bar, text="no file loaded")
        self.file_lbl.pack(side="left", padx=12)

    def _build_info(self):
        box = ttk.LabelFrame(self, text="EEPROM contents", padding=6)
        box.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.info = tk.Text(box, width=82, height=30, wrap="none", font=("Menlo", 11))
        self.info.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.info.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.info.configure(yscrollcommand=sb.set, state="disabled")

    def _build_actions(self):
        right = ttk.Frame(self)
        right.grid(row=1, column=1, sticky="nsew")

        gen = ttk.LabelFrame(right, text="Balance", padding=6)
        gen.pack(fill="x")
        ttk.Label(gen, text="Capacity [g]").grid(row=0, column=0, sticky="w")
        self.capacity = ttk.Entry(gen, width=10)
        self.capacity.grid(row=0, column=1, sticky="w")
        ttk.Label(gen, text="(override; normally read from the EEPROM)",
                  foreground="gray").grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(gen, text="Re-read", command=self._refresh).grid(row=0, column=2, padx=4)

        lin = ttk.LabelFrame(right, text="Linearity (L0)", padding=6)
        lin.pack(fill="x", pady=6)
        ttk.Label(lin, text="Half-load error [g]").grid(row=0, column=0, sticky="w")
        self.lin_err = ttk.Entry(lin, width=10)
        self.lin_err.grid(row=0, column=1)
        ttk.Label(lin, text="Test load [g]").grid(row=1, column=0, sticky="w")
        self.lin_load = ttk.Entry(lin, width=10)
        self.lin_load.grid(row=1, column=1)
        ttk.Button(lin, text="Apply", command=self.do_lin).grid(row=2, column=0, columnspan=2,
                                                                sticky="ew", pady=(4, 0))
        ttk.Label(lin, text="reading - true value at half load,\nafter zero and span calibration",
                  foreground="gray").grid(row=3, column=0, columnspan=2, sticky="w")

        tc = ttk.LabelFrame(right, text="Temperature compensation", padding=6)
        tc.pack(fill="x")
        ttk.Label(tc, text="Span drift [ppm/degC]").grid(row=0, column=0, sticky="w")
        self.tc_span = ttk.Entry(tc, width=10)
        self.tc_span.grid(row=0, column=1)
        ttk.Button(tc, text="Apply to S1", command=self.do_tcspan).grid(row=1, column=0,
                                                                        columnspan=2, sticky="ew")
        ttk.Label(tc, text="Zero drift [g/degC]").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.tc_zero = ttk.Entry(tc, width=10)
        self.tc_zero.grid(row=2, column=1, pady=(6, 0))
        ttk.Button(tc, text="Apply to Z1", command=self.do_tczero).grid(row=3, column=0,
                                                                        columnspan=2, sticky="ew")
        ttk.Label(tc, text="+ means the reading rises\nwhen the balance gets warmer",
                  foreground="gray").grid(row=4, column=0, columnspan=2, sticky="w")

        stk = ttk.LabelFrame(right, text="Parameter sticker", padding=6)
        stk.pack(fill="x")
        ttk.Button(stk, text="Load cell parameters from sticker...",
                   command=self.do_sticker).pack(fill="x")
        ttk.Label(stk, text="writes lines 00-20 into the open dump;\n"
                            "the sticker's own checksum verifies your typing",
                  foreground="gray").pack(anchor="w")

        typ = ttk.LabelFrame(right, text="Type data", padding=6)
        typ.pack(fill="x", pady=6)
        ttk.Label(typ, text="Calibration weight [g]").grid(row=0, column=0, sticky="w")
        self.calw = ttk.Entry(typ, width=10)
        self.calw.grid(row=0, column=1)
        ttk.Button(typ, text="Apply", command=self.do_calweight).grid(row=1, column=0,
                                                                      columnspan=2, sticky="ew")
        ttk.Button(typ, text="DeltaRange: fine increment over whole range",
                   command=self.do_fine).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        raw = ttk.LabelFrame(right, text="Direct value edit", padding=6)
        raw.pack(fill="x")
        self.sel = ttk.Combobox(raw, width=8, state="readonly",
                                values=list(pm.OTHER) + list(pm.CELL))
        self.sel.grid(row=0, column=0)
        self.sel.bind("<<ComboboxSelected>>", self._show_value)
        self.val = ttk.Entry(raw, width=12)
        self.val.grid(row=0, column=1, padx=4)
        ttk.Button(raw, text="Set", command=self.do_set).grid(row=0, column=2)
        self.sel_desc = ttk.Label(raw, text="", foreground="gray", wraplength=240)
        self.sel_desc.grid(row=1, column=0, columnspan=3, sticky="w")

    def _build_log(self):
        box = ttk.LabelFrame(self, text="Changes", padding=6)
        box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        box.columnconfigure(0, weight=1)
        self.log = tk.Text(box, height=7, wrap="word", font=("Menlo", 11))
        self.log.grid(row=0, column=0, sticky="ew")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ helpers
    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def need_file(self):
        if self.data is None:
            messagebox.showwarning("No file", "Open an EEPROM dump first.")
            return False
        return True

    def cap(self):
        text = self.capacity.get().strip()
        return float(text) if text else None

    def need_R(self):
        return pm.raw_counts_per_g(self.data, self.cap())

    def num(self, entry, what):
        try:
            return float(entry.get().strip().replace(",", "."))
        except ValueError:
            raise pm.PmError(f"{what}: enter a number.")

    def changed(self, name, old, new):
        self.dirty = True
        self.say(f"{name}: {old} -> {new}  ({new - old:+d})")
        self._refresh()

    def guard(fn):
        """show library errors in a dialog instead of crashing"""
        def wrapper(self, *a, **kw):
            if not self.need_file():
                return
            try:
                return fn(self, *a, **kw)
            except pm.PmError as e:
                messagebox.showerror("Not possible", str(e))
        return wrapper

    # ------------------------------------------------------------ file
    def do_open(self, path=None):
        path = path or filedialog.askopenfilename(
            title="Open 93C46 dump", filetypes=[("EEPROM dump", "*.BIN *.bin"), ("All files", "*")])
        if not path:
            return
        try:
            self.data, self.swapped, _ = pm.load(path)
        except (pm.PmError, OSError) as e:
            messagebox.showerror("Cannot read file", str(e))
            return
        self.original = bytearray(self.data)
        self.path = path
        self.dirty = False
        self.capacity.delete(0, "end")
        self.say(f"opened {os.path.basename(path)} "
                 f"({'swapped' if self.swapped else '8051'} byte order)")
        self._refresh()

    def do_save(self):
        if not self.need_file():
            return
        out = filedialog.asksaveasfilename(
            title="Save modified dump", defaultextension=".BIN",
            initialfile=os.path.basename(self.path or "eeprom.BIN"),
            filetypes=[("EEPROM dump", "*.BIN *.bin")])
        if not out:
            return
        if self.path and os.path.abspath(out) == os.path.abspath(self.path):
            if not messagebox.askyesno("Overwrite original?",
                                       "This overwrites the file you opened.\n"
                                       "Keep an untouched copy of the original dump!\n\nContinue?"):
                return
        data = bytearray(self.data)
        pm.fix_checksums(data)
        try:
            pm.save(out, data, self.swapped)
        except OSError as e:
            messagebox.showerror("Cannot write file", str(e))
            return
        self.data = data
        self.dirty = False
        self.say(f"saved {os.path.basename(out)} (checksums recomputed)")
        self._refresh()

    def do_revert(self):
        if not self.need_file():
            return
        self.data = bytearray(self.original)
        self.dirty = False
        self.say("reverted to the file as opened")
        self._refresh()

    # ------------------------------------------------------------ actions
    @guard
    def do_lin(self):
        err = self.num(self.lin_err, "Half-load error")
        load = self.num(self.lin_load, "Test load")
        if load <= 0:
            raise pm.PmError("Test load must be greater than zero.")
        R = self.need_R()
        delta = round(4 * (err / load) * 2 ** 48 / (R * load))
        a = pm.CELL["L0"][0]
        old = pm.g24(self.data, a)
        pm.s24(self.data, a, old + delta)
        self.say(f"using ~{R:.0f} raw counts/g; the half-load reading should move {-err:+g} g "
                 f"after recalibration")
        self.changed("L0", old, old + delta)

    @guard
    def do_tcspan(self):
        ppm = self.num(self.tc_span, "Span drift")
        delta = round(-ppm * 1e-6 * 2 ** 40 / pm.T_COUNTS_PER_C)
        a = pm.CELL["S1"][0]
        old = pm.g24(self.data, a)
        pm.s24(self.data, a, old + delta)
        self.changed("S1", old, old + delta)

    @guard
    def do_tczero(self):
        gpc = self.num(self.tc_zero, "Zero drift")
        R = self.need_R()
        delta = round(-gpc * R * 2 ** 16 / pm.T_COUNTS_PER_C)
        a = pm.CELL["Z1"][0]
        old = pm.g24(self.data, a)
        pm.s24(self.data, a, old + delta)
        self.changed("Z1", old, old + delta)

    @guard
    def do_calweight(self):
        grams = self.num(self.calw, "Calibration weight")
        u, digit = pm.units(self.data, self.cap())
        old, _ = pm.type_value(self.data, self.data[0x25], self.data[0x26])
        new = round(grams / digit)
        if abs(new * digit - grams) > digit / 2:
            raise pm.PmError(f"{grams} g is not a whole number of {digit} g display steps.")
        r7, r6 = pm.encode_pair(new, (self.data[0x26] >> 4) & 7, self.data[0x25] & 0x80)
        self.data[0x25], self.data[0x26] = r7, r6
        self.dirty = True
        self.say(f"calibration weight: {old * digit:g} g -> {new * digit:g} g "
                 f"(accepted within about +/-{new * digit / 32:g} g)")
        self._refresh()

    @guard
    def do_fine(self):
        done = False
        for pair in (0x21, 0x23):
            val, step = pm.type_value(self.data, self.data[pair], self.data[pair + 1])
            if val and step > 1:
                self.data[pair + 1] &= 0x8F
                self.say(f"range block {pair:02X}: above {val} steps the increment was {step}, now 1")
                done = True
        if not done:
            raise pm.PmError("No coarse secondary range found - this type already displays "
                             "its finest increment.")
        self.dirty = True
        self._refresh()

    @guard
    def do_sticker(self):
        StickerDialog(self)

    def apply_sticker(self, words):
        """words: the 21 sticker lines, already reduced to their low 16 bits"""
        trial = bytearray(self.data)
        for i, w in enumerate(words):
            trial[0x3E + 2 * i] = w & 0xFF
            trial[0x3E + 2 * i + 1] = w >> 8
        good = pm.ck(trial, 0x3E, 0x2A) == 0
        if not good:
            off = pm.ck(trial, 0x3E, 0x2A)
            if not messagebox.askyesno(
                    "Checksum does not match",
                    "The 21 numbers do not satisfy the sticker's own checksum "
                    f"(off by {off}).\n\nAlmost certainly one of them is mistyped - the balance "
                    "would run with wrong cell parameters.\n\nUse them anyway?"):
                return False
        self.data = trial
        self.dirty = True
        if good:
            self.say("sticker applied - checksum matches, transcription verified")
        else:
            self.say("sticker applied - WARNING: checksum does NOT match, check your typing")
        self._refresh()
        return True

    def _show_value(self, _event=None):
        if self.data is None:
            return
        name = self.sel.get()
        a, desc = (pm.CELL.get(name) or pm.OTHER[name])
        self.val.delete(0, "end")
        self.val.insert(0, str(pm.g24(self.data, a)))
        self.sel_desc.configure(text=f"[{a:02X}] {desc}")

    @guard
    def do_set(self):
        name = self.sel.get()
        if not name:
            raise pm.PmError("Pick a value to edit first.")
        a, _ = (pm.CELL.get(name) or pm.OTHER[name])
        try:
            new = int(self.val.get().strip(), 0)
        except ValueError:
            raise pm.PmError("Enter a whole number.")
        old = pm.g24(self.data, a)
        pm.s24(self.data, a, new)
        self.changed(name, old, new)

    # ------------------------------------------------------------ display
    def _refresh(self):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        if self.data is None:
            self.info.insert("end", "Open a 128-byte 93C46 dump to begin.\n")
        else:
            self.info.insert("end", self._describe())
        self.info.configure(state="disabled")
        name = os.path.basename(self.path) if self.path else "no file loaded"
        self.file_lbl.configure(text=name + ("  (modified)" if self.dirty else ""))

    def _describe(self):
        x, out = self.data, []
        cap_g = self.cap()
        out.append(f"model code [05] 0x{x[5]:02X}      ID {x[0x0E]:02X}{x[0x0F]:02X}{x[0x10]:02X}"
                   f"      type check [68] 0x{x[0x68]:02X}")
        marks = []
        for name, (a, n) in pm.BLOCKS.items():
            ok = pm.ck(x, a, n) == 0
            marks.append(f"{name} {'OK' if ok else 'BAD'}")
        out.append("checksums: " + "   ".join(marks)
                   + "\n           (recomputed automatically when you save)")
        cap, _ = pm.type_value(x, x[0x15], x[0x16])
        cal, _ = pm.type_value(x, x[0x25], x[0x26])
        u, digit = pm.units(x, cap_g)
        out.append(f"\ncapacity {cap * digit:g} g    calibration weight {cal * digit:g} g    "
                   f"display step {digit:g} g")
        for pair in (0x21, 0x23):
            val, step = pm.type_value(x, x[pair], x[pair + 1])
            if val:
                out.append(f"  secondary range: above {val * digit:g} g the step is "
                           f"{step * digit:g} g")
        out.append("\nStored values")
        for name, (a, desc) in {**pm.OTHER, **pm.CELL}.items():
            out.append(f"  {name:5s} [{a:02X}] {pm.g24(x, a):9d}   {desc}")
        T = pm.T_COUNTS_PER_C
        out.append("\nDerived")
        out.append(f"  TC steps {(pm.g24(x,0x43)-pm.g24(x,0x46))/T:+.1f} / 0 / "
                   f"{(pm.g24(x,0x49)-pm.g24(x,0x46))/T:+.1f} degC around T0")
        out.append(f"  span TC compensation {pm.g24(x,0x58)*T/2**40*1e6:+.1f} ppm/degC")
        if u:
            R = pm.raw_counts_per_g(x, cap_g)
            out.append(f"  ~{R:.0f} raw counts/g (estimate)")
            out.append(f"  zero TC compensation {pm.g24(x,0x4F)*T/2**16/R*1000:+.2f} mg/degC (estimate)")
            full = R * pm.capacity_g(x, cap_g)
            out.append(f"  linearity bow at half load "
                       f"{-pm.g24(x,0x5E)*full*full/4/2**48/R*1000:+.2f} mg (estimate)")
        out.append("\nParameter sticker (lines 00-20, low 16 bits; check digit not computed)")
        words = pm.sticker_words(x)
        for i in range(0, 21, 3):
            out.append("  " + "   ".join(f"{j:02d}: {words[j]:5d}" for j in range(i, min(i + 3, 21))))
        return "\n".join(out) + "\n"


class StickerDialog(tk.Toplevel):
    """type in the 21 numbers printed on the parameter sticker"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Load cell parameters from the parameter sticker")
        self.transient(app.winfo_toplevel())
        frm = ttk.Frame(self, padding=8)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left", text=(
            "Type the 21 numbers of lines 00 to 20, one per line or separated by spaces.\n"
            "Leading line labels are ignored, and the leading check digit of each printed\n"
            "number is stripped automatically.\n\n"
            "This replaces the cell parameters only - span, linearity and temperature\n"
            "compensation. The balance type must already be in the open dump, so start from\n"
            "a dump of the same model if you are programming a blank EEPROM.")
        ).pack(anchor="w")
        self.text = tk.Text(frm, width=34, height=14, font=("Menlo", 11))
        self.text.pack(fill="both", expand=True, pady=6)
        self.text.focus_set()
        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Button(row, text="Apply", command=self.apply).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        self.status = ttk.Label(frm, text="", foreground="gray", wraplength=360)
        self.status.pack(anchor="w", pady=(4, 0))

    def parse(self):
        words = []
        for token in self.text.get("1.0", "end").replace(",", " ").split():
            token = token.split(":")[-1].strip()      # allow "03:" style labels
            if not token:
                continue
            if not token.isdigit():
                raise pm.PmError(f"'{token}' is not a number.")
            value = int(token)
            if value > 0xFFFFF:
                raise pm.PmError(f"{value} is too large for a sticker line (max 1048575).")
            words.append(value & 0xFFFF)              # drop the printed check digit
        if len(words) != 21:
            raise pm.PmError(f"Expected 21 numbers, got {len(words)}.")
        return words

    def apply(self):
        try:
            words = self.parse()
        except pm.PmError as e:
            self.status.configure(text=str(e), foreground="#b00")
            return
        if self.app.apply_sticker(words):
            self.destroy()


def main():
    root = tk.Tk()
    root.title("Mettler AM/PM parameter EEPROM editor")
    app = App(root)
    if len(sys.argv) > 1:
        app.do_open(sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
