"""Win32 shell helpers: z-order checks for the taskbar overlay and WinEvent hooks."""

from __future__ import annotations

import ctypes
import itertools
import os
import threading
from ctypes import POINTER, Structure, byref, c_int, c_long, c_void_p, windll, wintypes


# Start/Search flyouts live in this window class; do not fight them for z-order.
SHELL_FLYOUT_CLASSES = {"Windows.UI.Core.CoreWindow"}

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SW_SHOWNOACTIVATE = 4
SWP_NOOWNERZORDER = 0x0200
GW_HWNDPREV = 3
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A
WM_DISPLAYCHANGE = 0x007E
SPI_SETWORKAREA = 0x002F
RDW_INVALIDATE = 0x0001
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
RDW_FRAME = 0x0400

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
OBJID_WINDOW = 0
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


def window_rect(hwnd):
    """Return the real (left, top, right, bottom) of a window, or None."""
    user32 = windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(Rect)]
    rect = Rect()
    if not hwnd or not user32.GetWindowRect(hwnd, byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def move_window(hwnd, x, y, width, height):
    """Place a window exactly, bypassing any geometry the toolkit has cached.

    Tk ignores a repeated identical ``geometry`` string, so after Windows moves
    the window behind its back only a direct SetWindowPos puts it back.
    """
    user32 = windll.user32
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, c_int, c_int, c_int, c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    return bool(hwnd and user32.SetWindowPos(hwnd, None, int(x), int(y), int(width), int(height), flags))


def ensure_visible(hwnd):
    """Show a window without activating it if something hid or minimised it.

    Returns True when it had to be shown.
    """
    user32 = windll.user32
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, c_int]
    if not hwnd or (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd)):
        return False
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return True


def rects_intersect(a, b):
    """True when two (left, top, right, bottom) rectangles share any area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def force_repaint(hwnd):
    """Invalidate and synchronously repaint a window and its children."""
    user32 = windll.user32
    user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.HANDLE, wintypes.UINT]
    user32.RedrawWindow.restype = wintypes.BOOL
    flags = RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW | RDW_FRAME
    return bool(hwnd and user32.RedrawWindow(hwnd, None, None, flags))


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
    ours = window_rect(hwnd)
    if ours is None:
        return False
    covered = False
    above = user32.GetWindow(hwnd, GW_HWNDPREV)
    for _ in range(64):
        if not above:
            break
        rect = Rect()
        # Only a visible window that overlaps our area counts; a topmost window on
        # another monitor (or elsewhere on screen) is no reason to re-raise.
        if (
            user32.IsWindowVisible(above)
            and user32.GetWindowRect(above, byref(rect))
            and rect.right > rect.left
            and rect.bottom > rect.top
            and rects_intersect(ours, (rect.left, rect.top, rect.right, rect.bottom))
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

    DEFAULT_RANGES = (
        (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
        (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
    )

    def __init__(self, on_event, ranges=None, process_id=0, accept=None):
        """ranges: (low, high) event pairs; process_id: 0 = all processes;
        accept(hwnd, object_id) -> bool filters events before on_event."""
        self._on_event = on_event
        self._ranges = tuple(ranges or self.DEFAULT_RANGES)
        self._process_id = process_id
        self._accept = accept
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._installed = False
        # Hold a reference for the lifetime of the hook or the callback is collected.
        self._callback = _WINEVENTPROC(self._dispatch)

    @property
    def active(self):
        return self._installed

    def _dispatch(self, _hook, _event, hwnd, object_id, _child_id, _thread, _time):
        try:
            if self._accept is not None and not self._accept(hwnd, object_id):
                return
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
            for low, high in self._ranges:
                handle = user32.SetWinEventHook(
                    low, high, None, self._callback, self._process_id, 0,
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


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
KEYBOARD_UNION_SIZE = 32 if ctypes.sizeof(c_void_p) == 8 else 24


class KeyboardInput(Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [
        ("ki", KeyboardInput),
        ("padding", ctypes.c_byte * KEYBOARD_UNION_SIZE),
    ]


class Input(Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]


def send_control_number(number):
    if number not in (1, 2, 3):
        raise ValueError("Unsupported desktop shortcut")
    digit_vk = ord("0") + number
    events = (
        (VK_CONTROL, 0),
        (digit_vk, 0),
        (digit_vk, KEYEVENTF_KEYUP),
        (VK_CONTROL, KEYEVENTF_KEYUP),
    )
    inputs = (Input * len(events))()
    for item, (virtual_key, flags) in zip(inputs, events):
        item.type = INPUT_KEYBOARD
        item.ki = KeyboardInput(virtual_key, 0, flags, 0, 0)

    send_input = windll.user32.SendInput
    send_input.argtypes = [wintypes.UINT, POINTER(Input), c_int]
    send_input.restype = wintypes.UINT
    sent = send_input(len(inputs), inputs, ctypes.sizeof(Input))
    if sent != len(inputs):
        raise OSError(ctypes.get_last_error(), "Windows did not accept the desktop shortcut")


def primary_work_area():
    rect = Rect()
    if windll.user32.SystemParametersInfoW(0x0030, 0, byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, windll.user32.GetSystemMetrics(0), windll.user32.GetSystemMetrics(1) - 48


def primary_taskbar_height():
    _left, top, _right, bottom = primary_work_area()
    screen_height = windll.user32.GetSystemMetrics(1)
    bottom_height = max(0, screen_height - bottom)
    top_height = max(0, top)
    return max(40, bottom_height or top_height or 46)


def primary_taskbar_geometry():
    """Return the current primary taskbar height and the taskbar's top edge."""
    _left, _top, _right, work_bottom = primary_work_area()
    return primary_taskbar_height(), work_bottom


def primary_screen_size():
    """Return the primary screen dimensions in the same units used by Tk."""
    return max(1, windll.user32.GetSystemMetrics(0)), max(1, windll.user32.GetSystemMetrics(1))


def find_taskbar_start():
    """Return (taskbar_hwnd, start_hwnd, start_left) for the primary taskbar.

    start_hwnd/start_left are None when the Start button window is not found
    (Explorer restarting, taskbar hidden, ...); taskbar_hwnd is None then too.
    """
    user32 = windll.user32
    find_window = user32.FindWindowW
    find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    find_window.restype = wintypes.HWND
    taskbar = find_window("Shell_TrayWnd", None)
    if not taskbar:
        return None, None, None

    get_class = user32.GetClassNameW
    get_class.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_class.restype = ctypes.c_int
    get_rect = user32.GetWindowRect
    get_rect.argtypes = [wintypes.HWND, POINTER(Rect)]
    get_rect.restype = wintypes.BOOL
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(child, _data):
        class_name = ctypes.create_unicode_buffer(96)
        if get_class(child, class_name, len(class_name)) and class_name.value == "Start":
            rect = Rect()
            if get_rect(child, byref(rect)) and rect.right > rect.left:
                found.append((rect.left, child))
        return True

    enumerate_children = user32.EnumChildWindows
    enumerate_children.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    enumerate_children.restype = wintypes.BOOL
    enumerate_children(taskbar, visit, 0)
    if not found:
        return taskbar, None, None
    left, start = min(found, key=lambda item: item[0])
    return taskbar, start, left


def primary_start_button_left():
    """Read the primary Windows taskbar Start button's current left edge."""
    return find_taskbar_start()[2]


class TaskbarWatcher:
    """Call on_change() when the taskbar or its Start button moves or resizes.

    Covers display/resolution/work-area changes (the taskbar is re-laid out) and
    icons being added or removed (the Start button shifts). Events arrive in
    bursts while Explorer settles, so callers must debounce. on_change runs on
    the hook thread and must only hand off to the UI thread.

    The hook is scoped to the Explorer process and filtered to two window
    handles, so unrelated shell activity costs almost nothing. retarget() must be
    called occasionally (a slow poll) because Explorer restarts change both
    handles; it re-installs the hook only when they changed.
    """

    def __init__(self, on_change):
        self._on_change = on_change
        self._hook = None
        self._targets = frozenset()
        self._signature = None

    @property
    def active(self):
        return bool(self._hook and self._hook.active)

    def _accept(self, hwnd, object_id):
        return object_id == OBJID_WINDOW and hwnd in self._targets

    @staticmethod
    def _lookup():
        taskbar, start, _left = find_taskbar_start()
        if not taskbar:
            return None
        pid = wintypes.DWORD()
        windll.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
        windll.user32.GetWindowThreadProcessId(taskbar, byref(pid))
        return taskbar, start, pid.value

    def start(self):
        """Install the hook; returns True when active."""
        found = self._lookup()
        if found is None:
            return False
        taskbar, start, pid = found
        self._targets = frozenset(h for h in (taskbar, start) if h)
        self._signature = found
        self._hook = WinEventHook(
            self._on_change,
            ranges=((EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE),),
            process_id=pid,
            accept=self._accept,
        )
        return self._hook.start()

    def retarget(self):
        """Re-install the hook if Explorer or the Start button window was replaced."""
        found = self._lookup()
        if found == self._signature and self.active:
            return False
        self.stop()
        return self.start()

    def stop(self):
        hook, self._hook = self._hook, None
        if hook is not None:
            hook.stop()


_WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WndClass(Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class DisplayChangeWatcher:
    """Call on_change() after Windows broadcasts a display or work-area change.

    WM_DISPLAYCHANGE (resolution, monitor added/removed/made primary) and
    WM_SETTINGCHANGE/SPI_SETWORKAREA (taskbar moved or resized) are sent to
    top-level windows only, so this owns a hidden top-level window with its own
    message loop on a private thread. on_change runs on that thread and must only
    hand off to the UI thread (never touch Tk here), exactly like WinEventHook.
    """

    CLASS_PREFIX = "CodexStatusPetDisplayWatcher"
    _counter = itertools.count(1)

    def __init__(self, on_change):
        self._on_change = on_change
        # One window class per instance: a class is bound to its wndproc (this instance),
        # so sharing a name between instances would route messages to the wrong callback.
        self._class_name = f"{self.CLASS_PREFIX}.{os.getpid()}.{next(self._counter)}"
        self._thread = None
        self._hwnd = None
        self._ready = threading.Event()
        self._active = False
        self._proc = _WNDPROC(self._wndproc)  # keep alive for the window's lifetime

    @property
    def active(self):
        return self._active

    @property
    def hwnd(self):
        """Handle of the hidden window (None until started); lets tests target this instance."""
        return self._hwnd

    def _wndproc(self, hwnd, message, wparam, lparam):
        user32 = windll.user32
        if message == WM_DISPLAYCHANGE or (message == WM_SETTINGCHANGE and wparam == SPI_SETWORKAREA):
            try:
                self._on_change()
            except Exception:
                pass
        elif message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        elif message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def start(self):
        if self._thread is not None:
            return self._active
        self._thread = threading.Thread(target=self._run, name="display-change-watch", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self._active

    def _run(self):
        user32 = windll.user32
        kernel32 = windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [POINTER(_WndClass)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            c_int, c_int, c_int, c_int,
            wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = c_int
        user32.DispatchMessageW.argtypes = [POINTER(wintypes.MSG)]
        instance = kernel32.GetModuleHandleW(None)
        registered = False
        try:
            wndclass = _WndClass()
            wndclass.lpfnWndProc = self._proc
            wndclass.hInstance = instance
            wndclass.lpszClassName = self._class_name
            registered = bool(user32.RegisterClassW(byref(wndclass)))
            if not registered:
                return
            # WS_POPUP, never shown: invisible top-level windows still receive broadcasts.
            self._hwnd = user32.CreateWindowExW(
                0x00000080, self._class_name, "", 0x80000000, 0, 0, 0, 0, None, None, instance, None
            )  # WS_EX_TOOLWINDOW keeps it out of Alt+Tab
            if not self._hwnd:
                return
            self._active = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(byref(message), None, 0, 0) > 0:
                user32.DispatchMessageW(byref(message))
        finally:
            self._active = False
            if registered:
                user32.UnregisterClassW(self._class_name, instance)
            self._ready.set()

    def stop(self):
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._hwnd:
            windll.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        thread.join(2)
        self._active = False
