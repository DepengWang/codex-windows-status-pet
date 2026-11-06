import ctypes
import sys
import threading
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api import win_shell_api


@unittest.skipUnless(sys.platform == "win32", "Win32 only")
class WinEventHookTests(unittest.TestCase):
    def test_hook_installs_and_removes_cleanly(self):
        hook = win_shell_api.WinEventHook(lambda: None)
        self.assertTrue(hook.start())
        self.assertTrue(hook.active)
        self.assertTrue(hook.start(), "second start must be a no-op")
        hook.stop()
        self.assertFalse(hook.active)
        hook.stop()  # idempotent

    def test_callback_errors_never_escape_into_the_message_loop(self):
        def boom():
            raise RuntimeError("boom")

        hook = win_shell_api.WinEventHook(boom)
        hook._dispatch(None, 3, None, 0, 0, 0, 0)  # must not raise

    def test_dispatch_forwards_events(self):
        calls = []
        hook = win_shell_api.WinEventHook(lambda: calls.append(1))
        hook._dispatch(None, 3, None, 0, 0, 0, 0)
        self.assertEqual(calls, [1])


class HookFilterTests(unittest.TestCase):
    def test_accept_predicate_filters_events_before_the_callback(self):
        calls = []
        hook = win_shell_api.WinEventHook(
            lambda: calls.append(1), accept=lambda hwnd, object_id: hwnd == 42 and object_id == 0
        )
        hook._dispatch(None, 0x800B, 41, 0, 0, 0, 0)  # wrong window
        hook._dispatch(None, 0x800B, 42, -4, 0, 0, 0)  # wrong object (caret, cursor, ...)
        self.assertEqual(calls, [])
        hook._dispatch(None, 0x800B, 42, 0, 0, 0, 0)
        self.assertEqual(calls, [1])


@unittest.skipUnless(sys.platform == "win32", "Win32 only")
class TaskbarWatcherTests(unittest.TestCase):
    def test_start_button_lookup_is_consistent(self):
        taskbar, start, left = win_shell_api.find_taskbar_start()
        self.assertEqual(left, win_shell_api.primary_start_button_left())
        if taskbar:
            self.assertEqual(start is None, left is None)

    def test_watcher_installs_retargets_idempotently_and_stops(self):
        watcher = win_shell_api.TaskbarWatcher(lambda: None)
        started = watcher.start()
        if not started:
            self.skipTest("no taskbar in this session")
        try:
            self.assertTrue(watcher.active)
            self.assertFalse(watcher.retarget(), "unchanged handles must not reinstall the hook")
            self.assertTrue(watcher.active)
        finally:
            watcher.stop()
        self.assertFalse(watcher.active)
        watcher.stop()  # idempotent

    def test_only_taskbar_and_start_windows_are_accepted(self):
        watcher = win_shell_api.TaskbarWatcher(lambda: None)
        watcher._targets = frozenset({10, 11})
        self.assertTrue(watcher._accept(10, 0))
        self.assertFalse(watcher._accept(12, 0))
        self.assertFalse(watcher._accept(10, -4))


class RectTests(unittest.TestCase):
    def test_rects_intersect_only_when_they_share_area(self):
        bar = (0, 1392, 900, 1440)
        self.assertTrue(win_shell_api.rects_intersect(bar, (0, 1392, 2560, 1440)))  # taskbar
        self.assertTrue(win_shell_api.rects_intersect(bar, (899, 1439, 1200, 1500)))  # 1 px overlap
        self.assertFalse(win_shell_api.rects_intersect(bar, (900, 1392, 1200, 1440)))  # touching edge
        self.assertFalse(win_shell_api.rects_intersect(bar, (2560, 0, 5120, 1440)))  # other monitor
        self.assertFalse(win_shell_api.rects_intersect(bar, (0, 0, 2560, 1392)))  # above the taskbar

    @unittest.skipUnless(sys.platform == "win32", "Win32 only")
    def test_window_rect_and_force_repaint_on_a_real_window(self):
        user32 = ctypes.windll.user32
        user32.GetDesktopWindow.restype = wintypes.HWND
        desktop = user32.GetDesktopWindow()
        left, top, right, bottom = win_shell_api.window_rect(desktop)
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)
        self.assertIsNone(win_shell_api.window_rect(None))
        self.assertIsInstance(win_shell_api.force_repaint(desktop), bool)
        self.assertFalse(win_shell_api.force_repaint(None))
        self.assertFalse(win_shell_api.ensure_visible(desktop), "already visible: nothing to do")
        self.assertFalse(win_shell_api.ensure_visible(None))
        self.assertFalse(win_shell_api.move_window(None, 0, 0, 10, 10))


@unittest.skipUnless(sys.platform == "win32", "Win32 only")
class DisplayChangeWatcherTests(unittest.TestCase):
    """Always target watcher.hwnd: a real overlay running on this machine owns a window
    of the same class, so looking it up by class name would hit the wrong one."""

    @staticmethod
    def send(watcher, message, wparam):
        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = wintypes.LPARAM
        user32.SendMessageW(watcher.hwnd, message, wparam, 0)

    @staticmethod
    def window_exists(hwnd):
        ctypes.windll.user32.IsWindow.argtypes = [wintypes.HWND]
        return bool(ctypes.windll.user32.IsWindow(hwnd))

    def test_display_and_workarea_changes_fire_the_callback_other_settings_do_not(self):
        fired = threading.Event()
        watcher = win_shell_api.DisplayChangeWatcher(fired.set)
        self.assertTrue(watcher.start())
        hwnd = watcher.hwnd
        self.assertTrue(self.window_exists(hwnd))
        try:
            self.send(watcher, win_shell_api.WM_DISPLAYCHANGE, 32)
            self.assertTrue(fired.wait(1))
            fired.clear()
            self.send(watcher, win_shell_api.WM_SETTINGCHANGE, win_shell_api.SPI_SETWORKAREA)
            self.assertTrue(fired.wait(1))
            fired.clear()
            self.send(watcher, win_shell_api.WM_SETTINGCHANGE, 0x2A)  # unrelated SPI_ action
            self.assertFalse(fired.wait(0.3))
        finally:
            watcher.stop()
        self.assertFalse(watcher.active)
        self.assertFalse(self.window_exists(hwnd), "hidden window must be destroyed on stop")
        watcher.stop()  # idempotent

    def test_callback_errors_do_not_kill_the_message_loop(self):
        calls = []

        def flaky():
            calls.append(1)
            raise RuntimeError("boom")

        watcher = win_shell_api.DisplayChangeWatcher(flaky)
        self.assertTrue(watcher.start())
        try:
            self.send(watcher, win_shell_api.WM_DISPLAYCHANGE, 32)
            self.send(watcher, win_shell_api.WM_DISPLAYCHANGE, 32)
            self.assertEqual(len(calls), 2)
        finally:
            watcher.stop()

    def test_two_watchers_can_coexist_and_restart(self):
        first = win_shell_api.DisplayChangeWatcher(lambda: None)
        second = win_shell_api.DisplayChangeWatcher(lambda: None)
        self.assertTrue(first.start())
        # The window class is already registered by `first` (as it would be by a running overlay):
        # a second watcher must still get its own window.
        self.assertTrue(second.start())
        self.assertNotEqual(first.hwnd, second.hwnd)
        first.stop()
        second.stop()
        third = win_shell_api.DisplayChangeWatcher(lambda: None)
        self.assertTrue(third.start(), "class must be reusable after all watchers stopped")
        third.stop()


class NeedsTopmostTests(unittest.TestCase):
    def test_flyout_foreground_suppresses_reassertion(self):
        with mock.patch.object(win_shell_api, "foreground_window_class", return_value="Windows.UI.Core.CoreWindow"):
            self.assertIn("Windows.UI.Core.CoreWindow", win_shell_api.SHELL_FLYOUT_CLASSES)
            self.assertNotIn("Shell_TrayWnd", win_shell_api.SHELL_FLYOUT_CLASSES)

    @unittest.skipUnless(sys.platform == "win32", "Win32 only")
    def test_desktop_window_is_not_reported_as_covered_by_invisible_helpers(self):
        user32 = ctypes.windll.user32
        user32.GetDesktopWindow.restype = wintypes.HWND
        # The desktop window has nothing above it, so it never needs re-raising.
        self.assertFalse(win_shell_api.window_needs_topmost(user32.GetDesktopWindow()))


if __name__ == "__main__":
    unittest.main()
