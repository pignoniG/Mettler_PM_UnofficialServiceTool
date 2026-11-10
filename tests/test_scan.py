"""line-settings scan of pm_serial.Link against a fake serial port that only answers at one baud rate and parity"""
import os
import queue
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
import pm_serial


class FakePort:
    """answers 'XV' like the service cassette, but only when baud rate and parity match; otherwise it
    hands back what a wrong baud rate produces: garbage"""

    def __init__(self, good, stream_only=False):
        self.good, self.stream_only = good, stream_only
        self.baudrate, self.parity = 9600, 'E'
        self.out = queue.Queue()
        self.sent = []

    def ok(self):
        return (self.baudrate, self.parity) == self.good

    def write(self, data):
        self.sent.append((self.baudrate, self.parity, data))
        if data.startswith(b'XV'):
            if not self.ok():
                self.out.put(b'\xf8\x80\x1e\r\n')
            elif self.stream_only:
                self.out.put(b'S    123.456 g\r\n')
            else:
                self.out.put(b'XV SVC 10.50 ID 874543\r\n')

    def read(self, n):
        try:
            return self.out.get(timeout=0.05)
        except queue.Empty:
            return b''

    def reset_input_buffer(self):
        while not self.out.empty():
            self.out.get_nowait()

    def close(self):
        pass


def make_link(port):
    link = pm_serial.Link.__new__(pm_serial.Link)
    link.p, link.log, link.settings, link.hits = port, lambda *a: None, (9600, 'E'), 0
    link.stream = pm_serial.Stream()
    link.q = queue.Queue()
    link._stop = threading.Event()
    link._reader = threading.Thread(target=link._read, daemon=True)
    link._reader.start()
    return link


class ScanTests(unittest.TestCase):
    def test_finds_2400_odd(self):
        link = make_link(FakePort((2400, 'O')))
        seen = []
        v = link.scan(seen.append)
        self.assertEqual(v, 'XV SVC 10.50 ID 874543')
        self.assertEqual(link.settings, (2400, 'O'))
        self.assertEqual(link.p.parity, 'O')
        self.assertTrue(seen[0].startswith('9600 baud, even'))
        link.close()

    def test_default_setting_is_tried_first(self):
        link = make_link(FakePort((9600, 'E')))
        seen = []
        link.scan(seen.append)
        self.assertEqual(len(seen), 1)
        link.close()

    def test_silent_balance(self):
        link = make_link(FakePort((300, 'M')))
        link.p.good = None
        with self.assertRaises(pm_serial.LinkError) as e:
            link.scan(settings=[(9600, 'E'), (2400, 'O')])
        self.assertIn('no answer at any of 2', str(e.exception))
        link.close()

    def test_readings_without_service_commands(self):
        link = make_link(FakePort((1200, 'E'), stream_only=True))
        with self.assertRaises(pm_serial.LinkError) as e:
            link.scan(settings=[(9600, 'E'), (1200, 'E')])
        self.assertIn('1200 baud, even', str(e.exception))
        self.assertIn('service commands', str(e.exception))
        self.assertEqual(link.settings, (1200, 'E'))
        link.close()


if __name__ == '__main__':
    unittest.main()
