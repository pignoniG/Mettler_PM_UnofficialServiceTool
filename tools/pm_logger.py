#!/usr/bin/env python3
"""
Log the continuous output of a Mettler AM/PM balance to CSV.

Balance configuration file, sector I-FACE: S = cONT, b = 9600, P = -e-, Pause = 0 (or 1), AU = on.
Cable: balance Data I/O pin 12 (RS out) -> PC RxD, pin 13 (RS COM) -> PC GND; true RS-232 adapter.

Each line from the balance looks like "S    294.378 g" (stable) or "SD   294.379 g" (moving).
The CSV gets: ISO time, seconds since start, status (S/SD/other), value, unit, raw line.

Usage:
  pm_logger.py                       # first USB serial port, 9600 7E1, file named by date/time
  pm_logger.py -p /dev/cu.X -o run.csv --minutes 180
  pm_logger.py --stable-only         # write only S lines
Stop with Ctrl-C; a summary of the stable readings is printed at the end.
"""
import argparse, csv, datetime, re, statistics, sys, time

try:
    import serial, serial.tools.list_ports
except ImportError:
    sys.exit("needs pyserial:  python3 -m pip install pyserial")

LINE = re.compile(r'^\s*(S|SD|SI|SIR|T)?\s*([-+]?\d+(?:\.\d+)?)\s*(\S+)?\s*$')


def find_port():
    ports = [p.device for p in serial.tools.list_ports.comports()
             if any(k in p.device.lower() for k in ('usb', 'ttyacm', 'ttys', 'com'))]
    if not ports:
        sys.exit("no serial port found - give one with -p")
    return ports[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-p', '--port', help='serial port (default: first USB serial port)')
    ap.add_argument('-b', '--baud', type=int, default=9600)
    ap.add_argument('--parity', default='E', choices=('E', 'O', 'N'), help='E (default), O or N')
    ap.add_argument('-o', '--out', help='CSV file (default: pm_log_YYYYmmdd_HHMMSS.csv)')
    ap.add_argument('--minutes', type=float, help='stop after this many minutes')
    ap.add_argument('--stable-only', action='store_true', help='write only stable (S) readings')
    ap.add_argument('--quiet', action='store_true', help='do not echo readings')
    a = ap.parse_args()

    port = a.port or find_port()
    out = a.out or datetime.datetime.now().strftime('pm_log_%Y%m%d_%H%M%S.csv')
    ser = serial.Serial(port, a.baud, bytesize=7 if a.parity != 'N' else 8,
                        parity=a.parity, stopbits=1, timeout=1)
    ser.reset_input_buffer()
    ser.readline()                      # drop the partial line we may have joined mid-way
    print(f"logging {port} at {a.baud} {7 if a.parity != 'N' else 8}{a.parity}1 to {out}  (Ctrl-C to stop)")

    t0 = time.time(); stable = []; n = 0; bad = 0
    with open(out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['time', 'seconds', 'status', 'value', 'unit', 'raw'])
        try:
            while True:
                if a.minutes and time.time() - t0 > a.minutes * 60:
                    break
                raw = ser.readline()
                if not raw:
                    continue
                txt = raw.decode('ascii', 'replace').strip()
                m = LINE.match(txt)
                now = time.time()
                if m:
                    status, value, unit = m.group(1) or '', float(m.group(2)), m.group(3) or ''
                else:
                    status, value, unit = 'other', '', ''
                    bad += 1
                if a.stable_only and status not in ('S', 'T'):
                    continue
                w.writerow([datetime.datetime.now().isoformat(timespec='milliseconds'),
                            f'{now - t0:.3f}', status, value, unit, txt])
                n += 1
                if status == 'S':
                    stable.append(value)
                if n % 25 == 0:
                    f.flush()
                if not a.quiet:
                    print(f"{now - t0:9.2f} s  {txt}")
        except KeyboardInterrupt:
            pass
    ser.close()

    print(f"\n{n} lines written to {out}" + (f", {bad} unparsed" if bad else ""))
    if len(stable) >= 2:
        last = stable[-min(len(stable), 60):]
        print(f"stable readings: {len(stable)}, last {len(last)}: mean {statistics.mean(last):.4f}, "
              f"std {statistics.stdev(last) * 1000:.2f} mg, span {(max(last) - min(last)) * 1000:.1f} mg")


if __name__ == '__main__':
    main()
