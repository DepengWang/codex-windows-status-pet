"""Active Windows virtual desktop name and a change-notification watcher."""

from __future__ import annotations

import ctypes
import threading
import uuid
import winreg
from ctypes import wintypes


VIRTUAL_DESKTOPS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"
DESKTOP_NAMES = ("主屏", "远程", "创作")
DESKTOP_NAME_ALIASES = {"主屏": "主屏", "远程": "远程", "编程": "创作", "创作": "创作"}

_REG_NOTIFY_CHANGE_NAME = 0x1
_REG_NOTIFY_CHANGE_LAST_SET = 0x4
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0


def read_active_virtual_desktop_name():
    """Map the Explorer current per-user virtual desktop ID to a display label."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, VIRTUAL_DESKTOPS_KEY) as key:
            current_id_data, _ = winreg.QueryValueEx(key, "CurrentVirtualDesktop")
            desktop_ids, _ = winreg.QueryValueEx(key, "VirtualDesktopIDs")
            if not isinstance(current_id_data, (bytes, bytearray)) or len(current_id_data) != 16:
                return None
            if not isinstance(desktop_ids, (bytes, bytearray)) or len(desktop_ids) % 16:
                return None

            current_id = uuid.UUID(bytes_le=bytes(current_id_data))
            active_ids = (
                uuid.UUID(bytes_le=bytes(desktop_ids[offset:offset + 16]))
                for offset in range(0, len(desktop_ids), 16)
            )
            if current_id not in active_ids:
                return None

            with winreg.OpenKey(key, f"Desktops\\{{{current_id}}}") as desktop_key:
                desktop_name, _ = winreg.QueryValueEx(desktop_key, "Name")
            return DESKTOP_NAME_ALIASES.get(str(desktop_name).strip())
    except (OSError, TypeError, ValueError):
        return None


class RegistryWatcher:
    """Call on_change() from a worker thread whenever a HKCU key changes.

    Blocks in the kernel on a wait handle, so it costs nothing while idle
    (unlike polling the registry every second). on_change must be cheap and
    thread-safe; it is only a wake-up signal, callers re-read the state.
    """

    def __init__(self, on_change, key_path=VIRTUAL_DESKTOPS_KEY, subtree=True):
        self._on_change = on_change
        self._key_path = key_path
        self._subtree = subtree
        self._stop_event = None
        self._thread = None
        self.started = threading.Event()  # set once the first wait is armed (or failed)
        self.active = False

    @staticmethod
    def _kernel32():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        return kernel32

    def start(self):
        kernel32 = self._kernel32()
        self._stop_event = kernel32.CreateEventW(None, True, False, None)
        if not self._stop_event:
            return False
        self._thread = threading.Thread(target=self._run, name="registry-watch", daemon=True)
        self._thread.start()
        self.started.wait(2)
        return self.active

    def stop(self):
        if self._stop_event:
            self._kernel32().SetEvent(self._stop_event)
        if self._thread:
            self._thread.join(2)

    def _run(self):
        kernel32 = self._kernel32()
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.RegNotifyChangeKeyValue.argtypes = [
            wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, wintypes.HANDLE, wintypes.BOOL,
        ]
        advapi32.RegNotifyChangeKeyValue.restype = ctypes.c_long

        change_event = kernel32.CreateEventW(None, False, False, None)
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._key_path, 0, winreg.KEY_NOTIFY | winreg.KEY_READ
            )
        except OSError:
            key = None
        if not change_event or key is None:
            if change_event:
                kernel32.CloseHandle(change_event)
            self.started.set()
            return

        handles = (wintypes.HANDLE * 2)(self._stop_event, change_event)
        flags = _REG_NOTIFY_CHANGE_NAME | _REG_NOTIFY_CHANGE_LAST_SET
        try:
            with key:
                while True:
                    # Re-arm before waiting so changes between wake-ups are not lost.
                    status = advapi32.RegNotifyChangeKeyValue(
                        int(key.handle), self._subtree, flags, change_event, True
                    )
                    if status != 0:
                        break
                    self.active = True
                    self.started.set()
                    if kernel32.WaitForMultipleObjects(2, handles, False, _INFINITE) != _WAIT_OBJECT_0 + 1:
                        break  # stop requested, or the wait failed
                    try:
                        self._on_change()
                    except Exception:
                        pass
        finally:
            self.active = False
            self.started.set()
            kernel32.CloseHandle(change_event)
