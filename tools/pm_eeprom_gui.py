#!/usr/bin/env python3
"""
Graphical front end for pm_eeprom.py - Mettler AM/PM series parameter EEPROM editor.

Run with:   python3 pm_eeprom_gui.py [dump.BIN]

Three tabs: "Dump file" edits a dump file offline (standard library only, tkinter); "Live balance" reads the EEPROM
of a connected balance, saves it to a file, applies the same edits as the file tab and writes the result (or any
opened file) back; "Procedures" runs the service manual's adjustments (calibration resistor check, linearity,
calibration, temperature check and compensation) from live readings and applies the result to the image of the
Live balance tab. The live tabs need pyserial and a balance with a cassette
that answers the service commands (see pm_serial.py); every write first saves a timestamped backup of the chip
into live_backups/ next to this file.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_eeprom as pm
import pm_serial
from pm_procedures_gui import Procedures

BACKUPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_backups")


class App(ttk.Frame):
    def __init__(self, master, shared_log=None):
        self.shared = shared_log                # one message strip for the whole window (None: this tab has its own)
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

        disp = ttk.LabelFrame(right, text="Display", padding=6)
        disp.pack(fill="x", pady=(0, 6))
        ttk.Label(disp, text="Deadband [units]").grid(row=0, column=0, sticky="w")
        self.deadband = ttk.Entry(disp, width=10)
        self.deadband.grid(row=0, column=1)
        ttk.Button(disp, text="Apply", command=self.do_deadband).grid(row=0, column=2, padx=4)
        ttk.Label(disp, text="Stability [increments]").grid(row=1, column=0, sticky="w")
        self.stability = ttk.Entry(disp, width=10)
        self.stability.grid(row=1, column=1)
        ttk.Button(disp, text="Apply", command=self.do_stability).grid(row=1, column=2, padx=4)
        ttk.Label(disp, text="deadband 0 shows every change; a larger\nstability window "
                             "helps taring/calibration",
                  foreground="gray").grid(row=2, column=0, columnspan=3, sticky="w")

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
        if self.shared is not None:
            return
        box = ttk.LabelFrame(self, text="Changes", padding=6)
        box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        box.columnconfigure(0, weight=1)
        self.log = tk.Text(box, height=7, wrap="word", font=("Menlo", 11))
        self.log.grid(row=0, column=0, sticky="ew")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ helpers
    def say(self, text):
        if self.shared is not None:
            self.shared.add(text)
            return
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
            initialfile=self.default_name(),
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

    def default_name(self):
        return os.path.basename(self.path or "eeprom.BIN")

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
        r7, r6 = pm.encode_pair(new, (self.data[0x26] >> 4) & 7, self.data[0x25] & 0x80,
                                    self.data[0x26] & 0x80)
        self.data[0x25], self.data[0x26] = r7, r6
        self.dirty = True
        self.say(f"calibration weight: {old * digit:g} g -> {new * digit:g} g "
                 f"(accepted within about +/-{new * digit / 32:g} g)")
        self._refresh()

    def whole(self, entry, what):
        v = self.num(entry, what)
        if v != int(v):
            raise pm.PmError(f"{what}: enter a whole number.")
        return int(v)

    @guard
    def do_deadband(self):
        msg = pm.set_deadband(self.data, self.whole(self.deadband, "Deadband"), self.cap())
        self.dirty = True
        self.say(msg)
        self._refresh()

    @guard
    def do_stability(self):
        msg = pm.set_stability(self.data, self.whole(self.stability, "Stability window"))
        self.dirty = True
        self.say(msg)
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
        out.append(f"mode [08] 0x{x[0x08]:02X}  {pm.mode_name(x[0x08])}")
        out.append("calibration lock [2B]: " +
                   ("on - the cassette jumper also locks calibration" if pm.blocking_on(x)
                    else "off - calibration always allowed"))
        out.append("  (a fitted jumper always locks the configuration file)")
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
        out.extend(pm.display_filter_lines(x, cap_g))
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


class Session:
    """the serial connection shared by the two live tabs; all balance traffic runs in a worker thread"""

    def __init__(self, root):
        self.root = root
        self.link = None
        self.version = None
        self.busy = False
        self.pending = None            # image of the last accepted write, for Verify
        self.cancel = False            # set by the Procedures tab to stop a running capture
        self.status = tk.StringVar(value="not connected")

    def connected(self):
        if self.link is None:
            messagebox.showwarning("Not connected", "Connect to the balance first (top bar).")
            return False
        return True

    def run(self, work, done, what):
        if self.busy:
            return
        self.busy = True
        self.status.set(what + "...")
        q = queue.Queue()

        def job():
            try:
                q.put(("ok", work()))
            except Exception as e:                      # shown to the user, never silently lost
                q.put(("err", e))
        threading.Thread(target=job, daemon=True).start()

        def poll():
            try:
                kind, val = q.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            self.busy = False
            self.status.set(self.version or "not connected")
            if kind == "ok":
                done(val)
            else:
                messagebox.showerror("Balance", str(val))
        poll()

    def connect(self, port, done=None):
        def work():
            link = pm_serial.Link(port)
            try:
                return link, link.connect()
            except Exception:
                link.close()
                raise

        def ok(res):
            self.link, self.version = res
            self.status.set(self.version)
            if done:
                done()
        self.disconnect()
        self.run(work, ok, "connecting")

    def disconnect(self):
        if self.link is not None:
            self.link.close()
        self.link = self.version = None
        self.status.set("not connected")

    def read(self, done):
        if self.connected():
            self.run(self.link.dump, done, "reading the EEPROM")

    def restart(self, say):
        if not self.connected():
            return
        if not messagebox.askyesno("Restart", "Restart the balance now?\nIt comes back in the -OFF- standby display: "
                                              "press the control bar to wake it."):
            return
        self.run(self.link.restart, lambda _r: say("restart sent; wake the balance with the control bar"), "restarting")

    def write(self, image, say, finished=None):
        """back up the chip, ask, write, then tell the user to wake the balance and verify"""
        if not self.connected():
            return
        img = bytearray(image)
        pm.fix_checksums(img)
        try:
            pm_serial.check_image(img)
        except pm_serial.LinkError as e:
            messagebox.showerror("Image refused", str(e))
            return

        def backup():
            chip = self.link.dump()
            os.makedirs(BACKUPS, exist_ok=True)
            path = os.path.join(BACKUPS, "eeprom_%s_%s.BIN" % (chip[0x0E:0x11].hex(), time.strftime("%Y%m%d_%H%M%S")))
            pm.save(path, chip, True)
            return chip, path

        def after_backup(res):
            chip, path = res
            say("backup of the chip saved: " + path)
            same = chip[0x0E:0x11] == img[0x0E:0x11]
            if same:
                ok = messagebox.askyesno("Write to the balance",
                                         "Replace the whole EEPROM of the balance (ID %s) with this image?\n\n"
                                         "This includes the user calibration.\nA backup was saved first." % chip[0x0E:0x11].hex())
            else:
                ok = messagebox.askyesno("Different ID",
                                         "The image has ID %s, the chip in the balance has ID %s.\n\n"
                                         "That is right for a board swap or a replaced EEPROM, wrong if you picked the "
                                         "wrong file: the balance would run with another scale's parameters.\n\n"
                                         "Write it anyway?" % (img[0x0E:0x11].hex(), chip[0x0E:0x11].hex()))
            if not ok:
                say("write cancelled")
                return
            self.run(lambda: self.link.load(img, override_id=not same), lambda _r: accepted(),
                     "writing the EEPROM (about 10 s)")

        def accepted():
            self.pending = img
            say("image accepted by the balance (ID %s); it restarts into -OFF-" % img[0x0E:0x11].hex())
            messagebox.showinfo("Written", "The balance accepted the image and restarts into the -OFF- standby display.\n\n"
                                           "Press the control bar to wake it, then use Verify.")
            if finished:
                finished(img)
        self.run(backup, after_backup, "backing up the chip")

    def verify(self, say):
        if not self.connected():
            return
        if self.pending is None:
            messagebox.showinfo("Verify", "No write to verify in this session.")
            return

        def done(chip):
            diff = [i for i in range(128) if chip[i] != self.pending[i]]
            if not diff:
                say("verified: the chip equals the image written")
            else:
                say("chip read back; bytes that differ from the image: " + ", ".join("%02X" % i for i in diff) +
                    ("   (0x36 and 0x39 are rewritten by the firmware at every restart)" if set(diff) <= {0x36, 0x39} else
                     "   CHECK THIS"))
        self.run(self.link.dump, done, "reading back")


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


def changed_parameters(a, b):
    """names of the 24-bit parameters (and the other single bytes) that differ between two images"""
    names = [n for n, (addr, _d) in {**pm.OTHER, **pm.CELL}.items() if pm.g24(a, addr) != pm.g24(b, addr)]
    covered = set()
    for n, (addr, _d) in {**pm.OTHER, **pm.CELL}.items():
        covered.update(range(addr, addr + 3))
    if any(a[i] != b[i] for i in range(128) if i not in covered and not 0x36 <= i <= 0x39):
        names.append("other bytes")
    return names


class LiveEditor(App):
    """the file editor working on the chip of the balance: read, edit with the same panels, write back"""

    def __init__(self, master, session, shared_log=None):
        self.s = session
        self.chip = None                    # what the chip held when last read or written
        self.read_at = None
        self.undo_stack, self._last, self._undoing = [], None, False
        super().__init__(master, shared_log)
        self.path = None
        self.file_lbl.configure(text="not read yet")
        self.after(300, self._tick_buttons)

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.btn = {}
        for key, text, cmd, pad in (("read", "Read from balance", self.do_read, 0), ("save", "Save to file...", self.do_save, 4),
                                    ("open", "Open file...", self.do_open, 0), ("write", "Write to balance", self.do_write, 4),
                                    ("undo", "Undo", self.undo, 0), ("revert", "Revert", self.do_revert, 4),
                                    ("verify", "Verify last write", lambda: self.s.verify(self.say), 0),
                                    ("restart", "Restart balance", lambda: self.s.restart(self.say), 4)):
            self.btn[key] = ttk.Button(bar, text=text, command=cmd)
            self.btn[key].pack(side="left", padx=(pad, 0))
        self.file_lbl = ttk.Label(bar, text="")
        self.file_lbl.pack(side="left", padx=12)

    def _build_log(self):
        pass                                    # messages go to the shared Changes strip at the bottom of the window

    def say(self, text):
        if self.shared is not None:
            self.shared.add(text)

    def _tick_buttons(self):
        link, have = self.s.link is not None, self.data is not None
        mod = have and self.modified()
        want = {"read": link, "write": link and have and mod, "verify": link and self.s.pending is not None,
                "restart": link, "save": have, "undo": bool(self.undo_stack), "revert": have and self.dirty}
        for k, on in want.items():
            self.btn[k].state(["!disabled" if on and not self.s.busy else "disabled"])
        self.after(300, self._tick_buttons)

    def modified(self):
        return self.dirty or (self.chip is not None and self.data is not None and bytes(self.data) != bytes(self.chip))

    def need_file(self):
        if self.data is None:
            messagebox.showwarning("Nothing read", "Read the EEPROM from the balance first.")
            return False
        return True

    def _refresh(self):
        if self.data is not None:
            cur = bytes(self.data)
            if self._last is not None and cur != self._last and not self._undoing:
                self.undo_stack.append(self._last)
            self._last = cur
        super()._refresh()

    def undo(self):
        if not self.undo_stack:
            return
        prev = self.undo_stack.pop()
        self._undoing = True
        self.data = bytearray(prev)
        self._refresh()
        self._undoing = False
        self.dirty = self.chip is None or bytes(self.data) != bytes(self.chip)
        self.say("undone: the image is back to the previous state")
        super()._refresh()

    def do_read(self):
        if self.modified() and not messagebox.askyesno("Discard changes?", "Reading replaces the edits not yet written. Continue?"):
            return

        def done(chip):
            self.data, self.original, self.swapped, self.dirty = chip, bytearray(chip), True, False
            self.chip, self.read_at = bytes(chip), time.strftime("%H:%M")
            self.undo_stack, self._last = [], None
            self.path = "balance %s" % chip[0x0E:0x11].hex()
            self.capacity.delete(0, "end")
            self.say("read the EEPROM of the balance (ID %s)" % chip[0x0E:0x11].hex())
            self._refresh()
        self.s.read(done)

    def default_name(self):
        return "eeprom_%s_%s.BIN" % (self.data[0x0E:0x11].hex(), time.strftime("%Y%m%d_%H%M%S"))

    def do_open(self, path=None):
        """load a file into the editor to write it to the balance (it counts as modified: it is not what the chip holds)"""
        before = self.path
        super().do_open(path)
        if self.path != before:
            self.dirty = True
            self.undo_stack, self._last = [], None
            self._refresh()

    def do_write(self):
        if not self.need_file():
            return

        def finished(img):
            self.data, self.original, self.dirty = bytearray(img), bytearray(img), False
            self.chip, self.read_at = bytes(img), time.strftime("%H:%M")
            self._refresh()
        self.s.write(self.data, self.say, finished)


class ChangeLog(ttk.LabelFrame):
    """one message strip for the whole window, so the result of an action is visible on every tab"""

    def __init__(self, master):
        super().__init__(master, text="Changes", padding=4)
        self.columnconfigure(0, weight=1)
        self.text = tk.Text(self, height=3, wrap="word", font=("Menlo", 11))
        self.text.grid(row=0, column=0, sticky="ew")
        self.text.configure(state="disabled")

    def add(self, text):
        self.text.configure(state="normal")
        self.text.insert("end", "%s  %s\n" % (time.strftime("%H:%M:%S"), text))
        self.text.see("end")
        self.text.configure(state="disabled")


class StatusHeader(ttk.Frame):
    """what the user needs to know before pressing anything: link, balance, image, and the one write button"""

    def __init__(self, master, session, editor):
        super().__init__(master, padding=(8, 4, 8, 0))
        self.s, self.ed = session, editor
        self.columnconfigure(0, weight=1); self.columnconfigure(1, weight=1)
        left = ttk.LabelFrame(self, text="Balance", padding=6); left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.dot = ttk.Label(left, text="\u25CF", font=("Helvetica", 16), foreground="#888888"); self.dot.grid(row=0, column=0, rowspan=2)
        self.conn = ttk.Label(left, text="not connected", font=("Helvetica", 12, "bold")); self.conn.grid(row=0, column=1, sticky="w")
        self.state = ttk.Label(left, text="", foreground="gray"); self.state.grid(row=1, column=1, sticky="w")
        self.activity = ttk.Label(left, text="", foreground="#185FA5"); self.activity.grid(row=0, column=2, padx=12)
        mid = ttk.LabelFrame(self, text="Working image", padding=6); mid.grid(row=0, column=1, sticky="nsew")
        mid.columnconfigure(0, weight=1)
        self.img_src = ttk.Label(mid, text="none yet", font=("Helvetica", 12, "bold")); self.img_src.grid(row=0, column=0, sticky="w")
        self.img_mod = ttk.Label(mid, text="", foreground="gray"); self.img_mod.grid(row=1, column=0, sticky="w")
        self.write = ttk.Button(mid, text="Write to balance", command=editor.do_write)
        self.write.grid(row=0, column=1, rowspan=2, padx=8)
        self.after(300, self.refresh)

    def refresh(self):
        link = self.s.link
        if link is None:
            self.dot.configure(foreground="#888888"); self.conn.configure(text="not connected")
            self.state.configure(text="pick the port above and press Connect")
        else:
            awake = link.stream.fresh()
            self.dot.configure(foreground="#3B6D11" if awake else "#BA7517")
            self.conn.configure(text="connected  " + (self.s.version or ""))
            self.state.configure(text="balance awake, streaming" if awake else
                                 "no readings arriving: asleep (-OFF-)? press the control bar")
        busy = self.s.status.get()
        self.activity.configure(text=busy if self.s.busy else "")
        ed = self.ed
        if ed.data is None:
            self.img_src.configure(text="none yet")
            self.img_mod.configure(text="read the EEPROM from the balance, or open a file (Live balance tab)")
        else:
            src = ("from the balance, read %s" % ed.read_at) if (ed.path or "").startswith("balance") else os.path.basename(ed.path or "")
            self.img_src.configure(text="%s  (ID %s)" % (src, ed.data[0x0E:0x11].hex()))
            if ed.modified():
                names = changed_parameters(ed.chip, ed.data) if ed.chip is not None else []
                self.img_mod.configure(text="modified, NOT written" + (": " + ", ".join(names) if names else ""), foreground="#854F0B")
            else:
                self.img_mod.configure(text="identical to the chip", foreground="gray")
        ok = link is not None and ed.data is not None and ed.modified() and not self.s.busy
        self.write.state(["!disabled" if ok else "disabled"])
        self.after(300, self.refresh)


class ConnectionBar(ttk.Frame):
    def __init__(self, master, session):
        super().__init__(master, padding=(8, 8, 8, 0))
        self.s = session
        ttk.Label(self, text="Serial port").pack(side="left")
        self.port = ttk.Combobox(self, width=34)
        self.port.pack(side="left", padx=4)
        ttk.Button(self, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(self, text="Connect", command=self.connect).pack(side="left", padx=4)
        ttk.Button(self, text="Disconnect", command=session.disconnect).pack(side="left")
        self.refresh()

    def refresh(self):
        ports = pm_serial.list_ports()
        self.port.configure(values=ports)
        if ports and not self.port.get():
            self.port.set(next((p for p in ports if "usb" in p.lower()), ports[0]))

    def connect(self):
        if not self.port.get():
            messagebox.showwarning("Port", "Pick a serial port (needs pyserial: pip install pyserial).")
            return
        self.s.connect(self.port.get())


def main():
    root = tk.Tk()
    root.title("Mettler AM/PM parameter EEPROM editor")
    root.minsize(1100, 940)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)
    session = Session(root)
    nb = ttk.Notebook(root)
    tabs = []
    for title in ("Dump file", "Live balance", "Procedures"):
        t = ttk.Frame(nb)
        nb.add(t, text=title)
        tabs.append(t)
    log = ChangeLog(root)
    app = App(tabs[0], log)
    live = LiveEditor(tabs[1], session, log)
    Procedures(tabs[2], session, live)
    ConnectionBar(root, session).grid(row=0, column=0, sticky="ew")
    StatusHeader(root, session, live).grid(row=1, column=0, sticky="ew")
    nb.grid(row=2, column=0, sticky="nsew")
    log.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
    if len(sys.argv) > 1:
        app.do_open(sys.argv[1])
    root.protocol("WM_DELETE_WINDOW", lambda: (session.disconnect(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
