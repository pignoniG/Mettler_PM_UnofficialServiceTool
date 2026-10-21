"""
Live EEPROM access over the balance's serial line, for balances fitted with a cassette that answers the
service commands (XV version, XD dump, XA/XB arm, XL load, XR restart).  The cassette itself is not part of this
repository; this file only speaks the protocol.

Needs pyserial (pip install pyserial) - imported only when a port is opened, so the file-only tools keep working
without it.  Line settings are those of the balance's I-FACE sector: 9600 baud, 7 data bits, even parity, 1 stop bit.

A successful write makes the balance restart into the -OFF- standby display, where it is silent until the
control bar is pressed.
"""
import queue
import re
import threading
import time

import pm_eeprom as pm

# lines the balance streams on its own; everything else is an answer to a command
WEIGHT = re.compile(rb'^(S|SD|SI) +([-+]?\d+\.\d+) *g?$')
TEMP = re.compile(rb'^T (\d+)$')
RAW = re.compile(rb'^R ([0-9A-F]{8}) ([0-9A-F]{6})(?: ([0-9A-F]{6}) ([0-9A-F]{6}) ([0-9A-F]{2}))?$')
OVERLOAD = re.compile(rb'^SI[+-]$')
PAUSE = bytes([0x00, 0x16, 0x1A, 0x26, 0x4B])


class LinkError(Exception):
    pass


def list_ports():
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    return [p.device for p in serial.tools.list_ports.comports()]


def hex_records(img):
    out = []
    for a in range(0, 128, 8):
        b = bytes([8, a >> 8, a & 255, 0]) + bytes(img[a:a + 8])
        out.append((':' + b.hex().upper() + '%02X' % ((-sum(b)) & 255) + '\r\n').encode())
    return out + [b':00000001FF\r\n']


def check_image(img):
    """raises LinkError unless the 128 bytes (8051 order) would be accepted by the balance's loader"""
    if len(img) != 128:
        raise LinkError('an image is 128 bytes')
    if (img[0x68] & 0xF8) != 0x60:
        raise LinkError('byte 0x68 is not a type-check byte (0x60..0x67)')
    for name, (a, n) in pm.BLOCKS.items():
        if name != 'usercal' and pm.ck(img, a, n) != 0:
            raise LinkError(f'{name} block checksum is wrong')


class Stream:
    """what the balance sends on its own: weight lines, T lines (temperature count) and the R lines of the service
    cassette (raw A/D value, and about once a second the temperature counts).  Updated by the reader thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.weight = None          # (grams, stable, time)
        self.temp = None            # (count, time)
        self.raw = None             # (raw value, time)
        self.stable = False
        self.serial = 0             # counts R lines; capture waits on it
        self._samples = []          # raw samples collected by a running capture: (raw, temp, weight)
        self._capturing = False

    def feed(self, line):
        """True if the line was one of the stream's"""
        now = time.time()
        m = WEIGHT.match(line)
        if m:
            with self.lock:
                self.stable = m.group(1) == b'S'
                self.weight = (float(m.group(2)), self.stable, now)
            return True
        m = TEMP.match(line)
        if m:
            with self.lock:
                self.temp = (int(m.group(1)), now)
            return True
        m = RAW.match(line)
        if m:
            raw = int(m.group(2), 16)
            if raw & 0x800000:
                raw -= 1 << 24
            with self.lock:
                if m.group(4):
                    self.temp = (int(m.group(4), 16), now)
                self.raw = (raw, now)
                self.serial += 1
                if self._capturing:
                    if self.stable and self.temp:
                        self._samples.append((raw, self.temp[0], self.weight[0] if self.weight else None))
                    else:
                        self._samples.clear()           # only an unbroken run of stable samples counts
            return True
        return bool(OVERLOAD.match(line))

    def snapshot(self):
        with self.lock:
            return dict(weight=self.weight, temp=self.temp, raw=self.raw, stable=self.stable)

    def fresh(self, max_age=3.0):
        with self.lock:
            return self.raw is not None and time.time() - self.raw[1] < max_age

    def capture(self, count=30, timeout=90.0, progress=None, cancel=None):
        """average `count` consecutive samples taken while the balance reports a stable weight.
        -> dict(raw mean, raw_std, temp mean (counts), weight mean (g) or None, n).  Run it in a worker thread."""
        with self.lock:
            self._samples = []
            self._capturing = True
        t0 = time.time()
        try:
            while time.time() - t0 < timeout:
                if cancel and cancel():
                    raise LinkError('cancelled')
                with self.lock:
                    n = len(self._samples)
                    got = list(self._samples) if n >= count else None
                if progress:
                    progress(n, count)
                if got:
                    raws = [g[0] for g in got]
                    mean = sum(raws) / len(raws)
                    std = (sum((r - mean) ** 2 for r in raws) / (len(raws) - 1)) ** 0.5
                    ws = [g[2] for g in got if g[2] is not None]
                    return dict(raw=mean, raw_std=std, temp=sum(g[1] for g in got) / len(got),
                                weight=sum(ws) / len(ws) if ws else None, n=len(got))
                time.sleep(0.05)
            with self.lock:
                seen = self.raw is not None
            raise LinkError('no stable run of %d samples within %.0f s' % (count, timeout) if seen else
                            'no R lines received: the capture needs a cassette ROM with the raw line (--raw) and the '
                            'balance awake')
        finally:
            with self.lock:
                self._capturing = False


class Link:
    def __init__(self, port_name, log=None):
        try:
            import serial
        except ImportError:
            raise LinkError('pyserial is not installed (pip install pyserial)')
        try:
            self.p = serial.Serial(port_name, 9600, bytesize=7, parity='E', stopbits=1, timeout=0.2)
        except (serial.SerialException, OSError) as e:
            raise LinkError(f'cannot open {port_name}: {e}')
        self.log = log or (lambda *a: None)
        self.stream = Stream()
        self.q = queue.Queue()                  # lines that are answers, not stream
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        buf = b''
        while not self._stop.is_set():
            try:
                d = self.p.read(256)
            except Exception:
                return
            if not d:
                continue
            buf += d
            while b'\n' in buf:
                raw, buf = buf.split(b'\n', 1)
                l = raw.replace(b'\r', b'').strip(PAUSE + b' ')
                if l and not self.stream.feed(l):
                    self.q.put(l)

    def close(self):
        self._stop.set()
        self._reader.join(1.0)
        try:
            self.p.close()
        except Exception:
            pass

    def lines(self, timeout=2.0, until=None):
        out, t0 = [], time.time()
        while time.time() - t0 < timeout:
            try:
                l = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            out.append(l)
            self.log('<', l)
            if until and until(l):
                return out
        return out

    def send(self, data):
        self.log('>', data)
        self.p.write(data)

    def unlock(self):
        """the receiver drops the first byte after boot or wake: send a newline and let things settle"""
        self.send(b'\n')
        time.sleep(0.4)
        self.drain()
        self.lines(0.6)

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def command(self, cmd, expect, timeout=3.0):
        self.send(cmd + b'\r\n')
        got = self.lines(timeout, until=lambda l: l.startswith(expect))
        for l in got:
            if l.startswith(expect):
                return l
        raise LinkError(f'no answer {expect.decode()!r} to {cmd.decode()!r}')

    def connect(self):
        """-> version line, e.g. 'XV SVC 10.45 ID 874543'"""
        self.drain()
        self.unlock()
        try:
            return self.command(b'XV', b'XV SVC').decode()
        except LinkError:
            raise LinkError('no answer from the balance. It is silent in the -OFF- standby display (press the '
                            'control bar), and it needs the receive wire connected and a cassette with the service '
                            'commands.')

    def dump(self):
        """-> bytearray, 128 bytes in 8051 order"""
        self.send(b'XD\r\n')
        data, eof = bytearray(), False
        for l in self.lines(8.0, until=lambda l: l == b':00000001FF'):
            if l == b':00000001FF':
                eof = True
                break
            if not l.startswith(b':'):
                continue
            try:
                raw = bytes.fromhex(l[1:].decode())
            except ValueError:
                raise LinkError(f'garbled record {l!r}')
            if sum(raw) & 0xFF or raw[0] != 8 or raw[3] != 0 or (raw[1] << 8 | raw[2]) != len(data):
                raise LinkError(f'bad record {l!r}')
            data += raw[4:12]
        if not eof or len(data) != 128:
            raise LinkError(f'incomplete dump ({len(data)} bytes)')
        return data

    def load(self, img, override_id=False):
        """write the whole EEPROM. Returns when the balance has accepted the image; it restarts by itself."""
        check_image(img)
        arm = b'XB' if override_id else b'XA'
        self.command(arm, arm)
        self.send(b'XL\r\n')
        got = self.lines(3.0, until=lambda l: l == b'XL' or l.startswith(b'EL'))
        if b'XL' not in got:
            raise LinkError(f'the balance did not accept the load: {got[:3]}')
        for l in hex_records(img):
            self.send(l)
            time.sleep(0.03)
        res = self.lines(8.0, until=lambda l: l.startswith(b'E'))
        if not res or res[-1].startswith(b'EL'):
            raise LinkError(f'the balance refused the image: {res[-2:]}')

    def tare(self, timeout=20.0):
        """tare the balance (the stock T command: waits for a stable reading) and wait until the weight reads zero.
        The balance sends no answer to T; the weight line returning to 0 is the confirmation."""
        self.send(b'T\r\n')
        t0 = time.time()
        while time.time() - t0 < timeout:
            w = self.stream.snapshot()['weight']
            if w and w[1] and abs(w[0]) < 0.5 and time.time() - w[2] < 1.0 and time.time() - t0 > 0.5:
                return
            time.sleep(0.2)
        raise LinkError('the balance did not return to zero within %.0f s: it only tares while the reading is stable' % timeout)

    def restart(self):
        self.command(b'XR', b'XR')
