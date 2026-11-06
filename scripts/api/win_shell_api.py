"""Win32 shell helpers: z-order checks for the taskbar overlay and WinEvent hooks."""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, Structure, byref, c_int, c_long, windll, wintypes


# Start/Search flyouts live in this window class; do not fight them for z-order.
SHELL_FLYOUT_CLASSES = {"Windows.UI.Core.CoreWindow"}

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
GW_HWNDPREV = 3

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WM_QUIT = 0x0012
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002


class Rect(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


def set_window_topmost(hwnd):
    if not hwnd:
        return False
    set_pos = windll.user32.SetWindowPos
    set_pos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        c_int,
        c_int,
        c_int,
        c_int,
        wintypes.UINT,
    ]
    set_pos.restype = wintypes.BOOL
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
    return bool(set_pos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags))


def toplevel_hwnd(widget_id):
    """Tk winfo_id() is the inner child window; z-order lives on its parent frame."""
    user32 = windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    return user32.GetParent(widget_id) or widget_id


def foreground_window_class():
    user32 = windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return ""
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    buffer = ctypes.create_unicode_buffer(96)
    user32.GetClassNameW(foreground, buffer, len(buffer))
    return buffer.value


def window_needs_topmost(hwnd):
    """True when a visible window covers ours and no Start/Search flyout is active.

    Re-asserting HWND_TOPMOST unconditionally fights Start/Search flyouts for z-order.
    Invisible topmost helpers (IME etc.) always sit above us and are ignored.
    """
    user32 = windll.user32
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(Rect)]
    covered = False
    above = user32.GetWindow(hwnd, GW_HWNDPREV)
    for _ in range(64):
        if not above:
            break
        rect = Rect()
        if (
            user32.IsWindowVisible(above)
            and user32.GetWindowRect(above, byref(rect))
            and rect.right > rect.left
            and rect.bottom > rect.top
        ):
            covered = True
            break
        above = user32.GetWindow(above, GW_HWNDPREV)
    if not covered:
        return False
    return foreground_window_class() not in SHELL_FLYOUT_CLASSES


_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)


class WinEventHook:
    """Call on_event() when the foreground window changes or windows minimise/restore.

    The hook lives on its own thread with a private message loop. Do NOT install
    it on the Tk thread: out-of-context callbacks would then fire inside Tcl's
    event dispatch with the GIL released, and touching tkinter from there aborts
    the interpreter. on_event runs on the hook thread and must only hand off
    (for example ``root.after(0, ...)``), never do real work.
    """

    EVENT_RANGES = (
        (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
        (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
    )

    def __init__(self, on_event):
        self._on_event = on_event
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._installed = False
        # Hold a reference for the lifetime of the hook or the callback is collected.
        self._callback = _WINEVENTPROC(self._dispatch)

    @property
    def active(self):
        return self._installed

    def _dispatch(self, _hook, _event, _hwnd, _object_id, _child_id, _thread, _time):
        try:
            self._on_event()
        except Exception:
            pass

    def start(self):
        if self._thread is not None:
            return self._installed
        self._thread = threading.Thread(target=self._run, name="winevent-hook", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self._installed

    def _run(self):
        user32 = windll.user32
        kernel32 = windll.kernel32
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            _WINEVENTPROC,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.UnhookWinEvent.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = c_int
        user32.PeekMessageW.argtypes = [
            POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
        ]
        user32.DispatchMessageW.argtypes = [POINTER(wintypes.MSG)]

        message = wintypes.MSG()
        # Create this thread's message queue before anyone posts WM_QUIT to it.
        user32.PeekMessageW(byref(message), None, 0, 0, 0)
        self._thread_id = kernel32.GetCurrentThreadId()
        handles = []
        try:
            for low, high in self.EVENT_RANGES:
                handle = user32.SetWinEventHook(
                    low, high, None, self._callback, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
                )
                if not handle:
                    return
                handles.append(handle)
            self._installed = True
            self._ready.set()
            while user32.GetMessageW(byref(message), None, 0, 0) > 0:
                user32.DispatchMessageW(byref(message))
        finally:
            self._installed = False
            for handle in handles:
                user32.UnhookWinEvent(handle)
            self._ready.set()

    def stop(self):
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._thread_id:
            windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(2)
        self._installed = False
