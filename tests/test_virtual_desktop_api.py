import sys
import threading
import unittest
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.virtual_desktop_api import DESKTOP_NAME_ALIASES, RegistryWatcher, read_active_virtual_desktop_name


TEST_KEY = r"Software\CodexStatusPetTest\RegistryWatcher"


@unittest.skipUnless(sys.platform == "win32", "registry notifications are Windows-only")
class RegistryWatcherTests(unittest.TestCase):
    def setUp(self):
        self.key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        self.changed = threading.Event()

    def tearDown(self):
        winreg.CloseKey(self.key)
        for path in (TEST_KEY, r"Software\CodexStatusPetTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass

    def on_change(self):
        self.changed.set()

    def test_value_change_wakes_callback_and_stop_ends_the_thread(self):
        watcher = RegistryWatcher(self.on_change, key_path=TEST_KEY)
        self.assertTrue(watcher.start())
        self.assertFalse(self.changed.wait(0.3), "must stay idle without registry changes")
        winreg.SetValueEx(self.key, "CurrentVirtualDesktop", 0, winreg.REG_DWORD, 1)
        self.assertTrue(self.changed.wait(2))
        self.changed.clear()
        winreg.SetValueEx(self.key, "CurrentVirtualDesktop", 0, winreg.REG_DWORD, 2)
        self.assertTrue(self.changed.wait(2), "watcher must re-arm after the first change")
        watcher.stop()
        self.assertFalse(watcher._thread.is_alive())
        self.assertFalse(watcher.active)

    def test_missing_key_reports_inactive_so_callers_keep_polling(self):
        watcher = RegistryWatcher(self.on_change, key_path=TEST_KEY + r"\DoesNotExist")
        self.assertFalse(watcher.start())
        watcher.stop()

    def test_aliases_map_to_the_displayed_names(self):
        self.assertEqual(DESKTOP_NAME_ALIASES["编程"], "创作")
        self.assertIn(read_active_virtual_desktop_name(), {None, "主屏", "远程", "创作"})


if __name__ == "__main__":
    unittest.main()
