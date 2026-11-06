import ctypes
import sys
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
