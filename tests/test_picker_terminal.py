"""Native ncurses regressions; isolated credentials and HTTP fixtures only."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = '''
import curses, fcntl, json, os, signal, struct, sys, termios
from pathlib import Path
from tests.test_usage_picker import UsagePicker
fixture = UsagePicker()
fixture.setUp()
S = fixture.S
result = {}
def resize(rows, columns):
    fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
    os.kill(os.getpid(), signal.SIGWINCH)
'''


class PickerTerminal(unittest.TestCase):
    @staticmethod
    def click(x, y):
        """SGR terminal press/release packet at 1-based coordinates."""
        return (f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m').encode()

    def terminal(self, body, keys=b""):
        with tempfile.TemporaryDirectory(prefix="hub-native-picker-") as directory:
            path = Path(directory, "result.json")
            script = PRELUDE + "\ntry:\n" + "\n".join(
                "    " + line for line in body.splitlines()) + '''
finally:
    fixture.tearDown()
Path(sys.argv[1]).write_text(json.dumps(result))
'''
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 140, 0, 0))
            child = subprocess.Popen([sys.executable, "-B", "-c", script, str(path)],
                cwd=ROOT, env=dict(os.environ, TERM="xterm-256color"),
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            wire = bytearray()
            try:
                pending = list(keys) if isinstance(keys, list) else []
                if keys and not pending:
                    os.write(master, keys)
                deadline = time.monotonic() + 8
                while child.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], .05)[0]:
                        try:
                            wire.extend(os.read(master, 65536))
                        except OSError:
                            break
                        while pending and pending[0][0] in wire:
                            _, packet = pending.pop(0)
                            os.write(master, packet)
                child.wait(timeout=2)
                while select.select([master], [], [], 0)[0]:
                    try:
                        wire.extend(os.read(master, 65536))
                    except OSError:
                        break
                self.assertEqual(child.returncode, 0, bytes(wire[-1500:]))
                return json.loads(path.read_text()), bytes(wire)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                os.close(master)

    def test_footer_uses_current_terminal_size_after_a_blocking_operation(self):
        result, wire = self.terminal('''
def run(screen):
    curses.curs_set(0)
    S._usage_pairs(curses)
    S._picker_footer(screen, 'Before operation')
    resize(14, 80)
    sys.stdout.write('\x1b]999;after-resize\x07')
    sys.stdout.flush()
    S._picker_footer(screen, 'After operation')
    result['size'] = screen.getmaxyx()
curses.wrapper(run)
''')
        self.assertEqual(result["size"], [14, 80])
        after_resize = wire.split(b"\x1b]999;after-resize\x07")[1]
        positions = re.findall(rb"\x1b\[(\d+);(\d+)H", after_resize)
        self.assertTrue(positions, "Expected actual terminal painting")
        self.assertTrue(all(1 <= int(y) <= 14 and 1 <= int(x) <= 80
                            for y, x in positions), positions)

    def test_picker_rebuilds_at_current_size_after_switching(self):
        result, wire = self.terminal('''
native_wrapper = curses.wrapper
class Screen:
    def __init__(self, real):
        self.real = real
        self.events = iter((10, ord('q')))
    def __getattr__(self, key):
        return getattr(self.real, key)
    def getch(self):
        result.setdefault('frames', []).append(self.real.getmaxyx())
        result['bottom'] = self.real.instr(self.real.getmaxyx()[0] - 1, 0).decode()
        return next(self.events)
def wrapper(run):
    return native_wrapper(lambda real: run(Screen(real)))
curses.wrapper = wrapper
def switch(kind, uuid):
    resize(14, 80)
    return 'Fixture switched'
S.switch = switch
S.cmd_ui([])
''', keys=b"\n")
        self.assertEqual(result["frames"], [[18, 140], [14, 80]])
        self.assertIn("Coffe <3", result["bottom"])

    def test_switch_return_repaints_unchanged_rows_after_external_screen_changes(self):
        result, wire = self.terminal('''
native_wrapper = curses.wrapper
class Screen:
    def __init__(self, real):
        self.real = real
        self.events = iter((10, ord('q')))
    def __getattr__(self, key):
        return getattr(self.real, key)
    def getch(self):
        return next(self.events)
curses.wrapper = lambda run: native_wrapper(lambda real: run(Screen(real)))
def switch(kind, uuid):
    sys.stdout.write('\x1b[2J\x1b[H\x1b]999;external-change\x07')
    sys.stdout.flush()
    return 'Fixture switched'
S.switch = switch
S.cmd_ui([])
''', keys=b"\n")
        after_switch = wire.split(b"\x1b]999;external-change\x07")[1]
        for name in (b"5 accounts", b"Alpha", b"Bravo", b"Charlie", b"Delta", b"Echo"):
            self.assertIn(name, after_switch)

    def test_raw_hyperlink_is_not_emitted_at_outdated_coordinates(self):
        result, wire = self.terminal('''
def run(screen):
    resize(14, 80)
    sys.stdout.write('\x1b]999;guard\x07')
    sys.stdout.flush()
    S._emit_donate_link(17, 131)
curses.wrapper(run)
''')
        after_resize = wire.split(b"\x1b]999;guard\x07")[1]
        self.assertNotIn(b"Coffe", after_resize)
        self.assertNotIn(b"\x1b]8;;https://", after_resize)

    def test_native_mouse_clicks_confirm_and_cancel_centered_modals(self):
        for column, confirmed in ((48, True), (56, False)):
            with self.subTest(confirmed=confirmed):
                result, wire = self.terminal('''
def run(screen):
    curses.curs_set(0)
    curses.mousemask(curses.BUTTON1_CLICKED)
    result['confirmed'] = S._confirm(screen, 'Use reset', 'Use a reset for Bravo ?',
                                     'Expires: 2099-10-22 19:09 UTC')
curses.wrapper(run)
''', keys=self.click(2, 2) + self.click(column, 12))
                self.assertEqual(result['confirmed'], confirmed)
                self.assertIn(b'Use a reset for Bravo', wire)

    def test_native_mouse_switches_from_usage_column_and_clicks_modal_enter(self):
        result, wire = self.terminal('''
def switch(kind, uuid):
    result['switched'] = [kind, uuid]
    return 'Fixture switched'
S.switch = switch
S.cmd_ui([])
result['expected'] = ['codex', fixture.profiles[1]['id']]
''', keys=[(b'Coffe', self.click(26, 7)),
           (b'Switch to Bravo', self.click(48, 12)), (b'Fixture switched', b'q')])
        self.assertEqual(result.get('switched'), result['expected'], wire[-3000:])
        self.assertIn(b'Switch to Bravo', wire)


if __name__ == "__main__":
    unittest.main()
