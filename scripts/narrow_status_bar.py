"""Two-provider per-user quota bar for the Windows fork."""

from __future__ import annotations

import json
import ctypes
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from ctypes import POINTER, Structure, byref, c_int, c_long, c_void_p, windll, wintypes
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont

from api.claude_usage_api import ClaudeUsageClient
from api.codex_transport_api import AppServer
from api.narrow_bar_logging_api import LOGGER, configure_logging, log_exception
from api.quota_format_api import earliest_future_expiry, reset_display_time
from api.quota_parse_api import parse_quota_payload


MAX_WIDTH = 920
REFRESH_WIDTH = 46
BG = "#101929"
FG = "#EDF1F9"
MUTED = "#97B1CC"
BORDER = "#4A5A66"
TRANSPARENT = "#010203"
CODEX_NAME = "#C61862"
CODEX_THIRD = "#205B4E"
CLAUDE_NAME = "#B4532A"
CLAUDE_THIRD = "#2E4E7A"
DOT_LIVE = "#35D1A8"
DOT_REFRESHING = "#61C6D8"
DOT_OFFLINE = "#EFB86B"
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "narrow-status-bar.json"
DEFAULT_X, DEFAULT_Y = -4, -48
REFRESH_INTERVAL_MS = 5 * 60 * 1000
WAKE_CHECK_INTERVAL_MS = 30 * 1000
TASKBAR_CHECK_INTERVAL_MS = 60 * 1000
WINDOW_CHECK_INTERVAL_MS = 5 * 1000


class Rect(Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long), ("bottom", c_long)]


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


def primary_start_button_left():
    """Read the primary Windows taskbar Start button's current left edge."""
    user32 = windll.user32
    find_window = user32.FindWindowW
    find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    find_window.restype = wintypes.HWND
    taskbar = find_window("Shell_TrayWnd", None)
    if not taskbar:
        return None

    get_class = user32.GetClassNameW
    get_class.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_class.restype = ctypes.c_int
    get_rect = user32.GetWindowRect
    get_rect.argtypes = [wintypes.HWND, POINTER(Rect)]
    get_rect.restype = wintypes.BOOL
    edges = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(child, _data):
        class_name = ctypes.create_unicode_buffer(96)
        if get_class(child, class_name, len(class_name)) and class_name.value == "Start":
            rect = Rect()
            if get_rect(child, byref(rect)) and rect.right > rect.left:
                edges.append(rect.left)
        return True

    enumerate_children = user32.EnumChildWindows
    enumerate_children.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    enumerate_children.restype = wintypes.BOOL
    enumerate_children(taskbar, visit, 0)
    return min(edges) if edges else None


def width_before_start_button(x, start_left=None, gap=8):
    """Cap the strip at the live Start button boundary, leaving a small gap."""
    if start_left is None:
        start_left = primary_start_button_left()
    if start_left is None:
        return 1
    return max(1, min(MAX_WIDTH, int(start_left - int(x) - gap)))


def column_layout(width):
    """Allocate the available width across the four data columns and refresh."""
    width = max(1, int(width))
    refresh = min(REFRESH_WIDTH, max(1, round(width * 0.075)))
    content = max(0, width - refresh)
    name = min(160, round(content * 0.21))
    primary = round(content * 0.22)
    weekly = round(content * 0.26)
    return (
        (0, name, name + primary, name + primary + weekly, width - refresh, width),
        refresh,
    )


def primary_screen_has_fullscreen_foreground():
    get_foreground = windll.user32.GetForegroundWindow
    get_foreground.restype = c_void_p
    foreground = get_foreground()
    if not foreground:
        return False
    rect = Rect()
    get_rect = windll.user32.GetWindowRect
    get_rect.argtypes = [c_void_p, POINTER(Rect)]
    get_rect.restype = c_int
    if not get_rect(foreground, byref(rect)):
        return False
    screen_width = windll.user32.GetSystemMetrics(0)
    screen_height = windll.user32.GetSystemMetrics(1)
    return (
        rect.left <= 1
        and rect.top <= 1
        and rect.right >= screen_width - 1
        and rect.bottom >= screen_height - 1
    )


def load_settings():
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return {
            "x": int(value["x"]),
            "y": int(value["y"]),
            "compact": bool(value.get("compact", False)),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"x": DEFAULT_X, "y": DEFAULT_Y, "compact": False}


def save_settings(value):
    """Persist position only; quota values and provider output stay in memory."""
    try:
        SETTINGS_PATH.write_text(
            json.dumps(
                {"x": int(value["x"]), "y": int(value["y"]), "compact": False},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _safe_window(window):
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    remaining = None
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        remaining = max(0, min(100, round(100 - float(used))))
    raw_reset = _epoch(window.get("resetsAt"))
    if remaining is None and raw_reset is None:
        return None
    return {"remainingPercent": remaining, "resetsAt": raw_reset}


def _codex_snapshot(parsed):
    limits = parsed.get("rateLimits", {})
    primary = _safe_window(limits.get("primary"))
    weekly = _safe_window(limits.get("secondary"))
    if primary is None and weekly is None:
        raise ValueError("No Codex quota windows")
    credits = parsed.get("rateLimitResetCredits", {})
    count = credits.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool):
        count = None
    expiry = earliest_future_expiry(credits.get("resetsAt"))
    return {
        "primary": primary,
        "weekly": weekly,
        "resetCount": count,
        "resetAt": expiry,
    }


def _claude_snapshot(parsed):
    def clean(value):
        if not isinstance(value, dict):
            return None
        remaining = value.get("remainingPercent")
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            return None
        reset = _epoch(value.get("resetsAt"))
        return {
            "remainingPercent": max(0, min(100, remaining)),
            "resetsAt": reset,
        }

    models = {}
    for name, value in parsed.get("models", {}).items():
        if name not in {"Sonnet", "Opus"}:
            continue
        model = clean(value)
        if model:
            models[name] = model
    primary = clean(parsed.get("primary"))
    weekly = clean(parsed.get("weekly"))
    if primary is None and weekly is None and not models:
        raise ValueError("No Claude quota windows")
    return {"primary": primary, "weekly": weekly, "models": models}


def _percent(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{max(0, min(100, round(value)))}%"


def _reset(value):
    return reset_display_time(value) if value is not None else "—"


def _safe_failure(provider, error):
    if isinstance(error, TimeoutError):
        return "query timed out"
    if isinstance(error, FileNotFoundError):
        return "CLI not found"
    if isinstance(error, PermissionError):
        return "process could not start"
    if isinstance(error, ValueError):
        return "no quota fields returned"
    if isinstance(error, RuntimeError):
        message = str(error)
        if "status " in message:
            suffix = message.rsplit("status ", 1)[-1]
            if suffix.isdigit():
                return f"CLI exited ({suffix})"
        return "local quota service unavailable"
    return f"request failed ({type(error).__name__})"


def _startup_shortcut_path():
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Codex Windows Status Pet.lnk"


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _create_startup_shortcut():
    link = _startup_shortcut_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    launch_vbs = root / "launch.vbs"
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise FileNotFoundError("PowerShell is unavailable")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$shell=New-Object -ComObject WScript.Shell; "
        f"$shortcut=$shell.CreateShortcut({_ps_quote(link)}); "
        "$shortcut.TargetPath=$env:SystemRoot+'\\System32\\wscript.exe'; "
        f"$shortcut.Arguments='//B //Nologo " + chr(34) + str(launch_vbs) + chr(34) + "'; "
        f"$shortcut.WorkingDirectory={_ps_quote(root)}; "
        "$shortcut.Save()"
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [powershell, "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not link.is_file():
        raise RuntimeError("Unable to create Startup shortcut")


class NarrowBar(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.bar_height, self.taskbar_y = primary_taskbar_geometry()
        self.start_button_left = primary_start_button_left()
        self.bar_width = width_before_start_button(
            self.settings["x"], start_left=self.start_button_left
        )
        self.column_edges, self.refresh_width = column_layout(self.bar_width)
        self.start_boundary_unavailable = self.start_button_left is None
        if self.bar_width == 1:
            LOGGER.warning("Start button boundary unavailable; overlay width minimized")
        self.title("Codex and Claude Usage")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=TRANSPARENT)
        self.settings["y"] = self.taskbar_y
        self.geometry(f"{self.bar_width}x{self.bar_height}+{self.settings['x']}+{self.taskbar_y}")
        self.resizable(False, False)

        self.closed = False
        self.hidden_for_fullscreen = False
        self.drag_origin = None
        self.drag_moved = False
        self.hovered = False
        self.background_visible = True
        self.hover_job = None
        self.tooltip_job = None
        self.tooltip = None
        self.menu = None
        self.startup_var = tk.BooleanVar(value=_startup_shortcut_path().is_file())
        self.codex_lock = threading.Lock()
        self.server = AppServer(queue.Queue(), client_version="1.1.0")
        self.server_started = False
        self.claude = ClaudeUsageClient()
        self.last_wake_wall = time.time()
        self.last_wake_mono = time.monotonic()
        self.last_action_error = None

        self.providers = {
            "Codex": {
                "state": "connecting",
                "snapshot": None,
                "updated_at": None,
                "error": None,
                "source": "Codex app-server account/rateLimits/read",
            },
            "Claude": {
                "state": "connecting",
                "snapshot": None,
                "updated_at": None,
                "error": None,
                "source": "Claude Code /usage",
            },
        }
        self.fetching = {"Codex": False, "Claude": False}

        self.canvas = tk.Canvas(
            self,
            width=self.bar_width,
            height=self.bar_height,
            bg=TRANSPARENT,
            bd=0,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.font_ui = tkfont.Font(self, family="Segoe UI", size=-13)
        self.font_bold = tkfont.Font(self, family="Segoe UI", size=-13, weight="bold")
        self.font_numbers = tkfont.Font(self, family="Consolas", size=-13)
        self.font_refresh = tkfont.Font(self, family="Segoe UI Symbol", size=-21)
        self.apply_transparency()
        self.draw_bar()

        self.canvas.bind("<ButtonPress-1>", self.begin_interaction)
        self.canvas.bind("<B1-Motion>", self.move_interaction)
        self.canvas.bind("<ButtonRelease-1>", self.end_interaction)
        self.canvas.bind("<Button-3>", self.open_menu_event)
        self.canvas.bind("<Button-2>", self.open_menu_event)
        self.canvas.bind("<Enter>", self.pointer_enter)
        self.canvas.bind("<Leave>", self.pointer_leave)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.after(100, self.refresh)
        self.after(REFRESH_INTERVAL_MS, self.auto_refresh)
        self.after(WAKE_CHECK_INTERVAL_MS, self.check_wake)
        self.after(TASKBAR_CHECK_INTERVAL_MS, self.ensure_taskbar_alignment)
        self.after(WINDOW_CHECK_INTERVAL_MS, self.check_fullscreen_state)

    def apply_transparency(self):
        try:
            self.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            # Keep text opaque on platforms where the Windows color key is unavailable.
            pass

    def draw_bar(self):
        if self.closed:
            return
        c = self.canvas
        c.delete("all")
        edges = self.column_edges
        width = self.bar_width
        mid = self.bar_height / 2
        row_centres = (self.bar_height / 4, self.bar_height * 3 / 4)

        if self.background_visible:
            self._rounded_rect(2.5, 2.5, width - 0.5, self.bar_height - 0.5, 6, "#080D15")
            self._rounded_rect(0.5, 0.5, width - 0.5, self.bar_height - 0.5, 6, BG)
            for row, y0 in enumerate((0, mid)):
                y1 = y0
                y2 = mid if row == 0 else self.bar_height
                c.create_rectangle(
                    edges[0] + 1,
                    y1 + (1 if row == 0 else 0),
                    edges[1],
                    y2,
                    fill=CODEX_NAME if row == 0 else CLAUDE_NAME,
                    outline="",
                )
                c.create_rectangle(
                    edges[2],
                    y1 + (1 if row == 0 else 0),
                    edges[3],
                    y2,
                    fill=CODEX_THIRD if row == 0 else CLAUDE_THIRD,
                    outline="",
                )
            for x in edges[1:-1]:
                c.create_line(x, 0, x, self.bar_height, fill=BORDER, width=1)
            c.create_line(0, mid, edges[-2], mid, fill=BORDER, width=1)
            self._draw_outer_frame_without_refresh_edges(edges[-2])

        self._draw_provider("Codex", 0, row_centres[0])
        self._draw_provider("Claude", 1, row_centres[1])
        c.create_text(
            width - self.refresh_width / 2,
            self.bar_height / 2,
            anchor="center",
            text="↻",
            fill=MUTED,
            font=self.font_refresh,
        )

    def _draw_outer_frame_without_refresh_edges(self, refresh_left):
        c = self.canvas
        left = 0.5
        top = 0.5
        bottom = self.bar_height - 0.5
        radius = 6
        right = left + 2 * radius

        c.create_arc(
            left, top, right, top + 2 * radius,
            start=90, extent=90, style="arc", outline=BORDER, width=1,
        )
        c.create_line(left + radius, top, refresh_left, top, fill=BORDER, width=1)
        c.create_line(left, top + radius, left, bottom - radius, fill=BORDER, width=1)
        c.create_arc(
            left, bottom - 2 * radius, right, bottom,
            start=180, extent=90, style="arc", outline=BORDER, width=1,
        )
        c.create_line(left + radius, bottom, refresh_left, bottom, fill=BORDER, width=1)

    def _rounded_rect(self, x1, y1, x2, y2, radius, fill, outline=""):
        points = (
            x1 + radius, y1, x2 - radius, y1, x2, y1 + radius,
            x2, y2 - radius, x2 - radius, y2, x1 + radius, y2,
            x1, y2 - radius, x1, y1 + radius,
        )
        self.canvas.create_polygon(
            *points,
            smooth=True,
            splinesteps=8,
            fill=fill,
            outline=outline,
            width=1 if outline else 0,
        )

    def _draw_segments(self, x, y, segments, width):
        measured = [(text, font, font.measure(text)) for text, font in segments]
        while measured and sum(part[2] for part in measured) > width:
            text, font, _size = measured[-1]
            if len(text) <= 1 or text == "…":
                measured.pop()
                continue
            text = text[:-2].rstrip() + "…" if len(text) > 2 else "…"
            measured[-1] = (text, font, font.measure(text))
        cursor = x
        for text, font, _size in measured:
            if not text:
                continue
            self.canvas.create_text(
                cursor,
                y,
                anchor="w",
                text=text,
                fill=FG,
                font=font,
            )
            cursor += font.measure(text)

    def _draw_provider(self, name, row, y):
        state = self.providers[name]
        edges = self.column_edges
        x0 = edges[0]
        dot_color = {
            "live": DOT_LIVE,
            "refreshing": DOT_REFRESHING,
            "connecting": DOT_REFRESHING,
            "offline": DOT_OFFLINE,
        }.get(state["state"], DOT_OFFLINE)
        self.canvas.create_oval(
            x0 + 11,
            y - 2.5,
            x0 + 16,
            y + 2.5,
            fill=dot_color,
            outline="",
        )
        label = name if state["state"] == "live" else f"{name} {state['state'].title()}"
        self.canvas.create_text(
            x0 + 23,
            y,
            anchor="w",
            text=label,
            fill=FG,
            font=self.font_bold,
            width=max(1, edges[1] - x0 - 30),
        )

        snapshot = state["snapshot"] or {}
        primary = snapshot.get("primary") or {}
        weekly = snapshot.get("weekly") or {}
        if name == "Codex":
            third_segments = [
                ("Reset ", self.font_ui),
                (_count_text(snapshot.get("resetCount")), self.font_numbers),
                (" times / ", self.font_ui),
                (_reset(snapshot.get("resetAt")), self.font_numbers),
            ]
            third_bg = CODEX_THIRD
            name_bg = CODEX_NAME
        else:
            third_segments = []
            for model_name in ("Sonnet", "Opus"):
                model = snapshot.get("models", {}).get(model_name)
                if not model:
                    continue
                if third_segments:
                    third_segments.append(("  •  ", self.font_ui))
                third_segments.extend([
                    (f"{model_name} ", self.font_ui),
                    (_percent(model.get("remainingPercent")), self.font_numbers),
                ])
            if not third_segments:
                third_segments = [("—", self.font_numbers)]
            third_bg = CLAUDE_THIRD
            name_bg = CLAUDE_NAME
        pad = min(12, max(5, round(self.bar_width * 0.012)))

        self._draw_segments(
            edges[1] + pad,
            y,
            [
                ("5h ", self.font_ui),
                (_percent(primary.get("remainingPercent")), self.font_numbers),
                (" / ", self.font_ui),
                (_reset(primary.get("resetsAt")), self.font_numbers),
            ],
            edges[2] - edges[1] - 2 * pad,
        )
        self._draw_segments(
            edges[2] + pad,
            y,
            [
                ("WEEK ", self.font_ui),
                (_percent(weekly.get("remainingPercent")), self.font_numbers),
                (" / ", self.font_ui),
                (_reset(weekly.get("resetsAt")), self.font_numbers),
            ],
            edges[3] - edges[2] - 2 * pad,
        )
        self._draw_segments(
            edges[3] + pad,
            y,
            third_segments,
            edges[4] - edges[3] - 2 * pad,
        )

    def _provider_detail_lines(self, name):
        state = self.providers[name]
        snapshot = state["snapshot"] or {}
        primary = snapshot.get("primary") or {}
        weekly = snapshot.get("weekly") or {}
        lines = [
            f"{name} — {state['state'].title()}",
            f"5h: {_percent(primary.get('remainingPercent'))} remaining; reset {_reset(primary.get('resetsAt'))}",
            f"Weekly: {_percent(weekly.get('remainingPercent'))} remaining; reset {_reset(weekly.get('resetsAt'))}",
        ]
        if name == "Codex":
            lines.append(
                f"Reset credits: {_count_text(snapshot.get('resetCount'))}; reset {_reset(snapshot.get('resetAt'))}"
            )
        else:
            models = snapshot.get("models", {})
            if models:
                for model_name in ("Sonnet", "Opus"):
                    model = models.get(model_name)
                    if model:
                        lines.append(
                            f"{model_name} weekly: {_percent(model.get('remainingPercent'))} remaining; "
                            f"reset {_reset(model.get('resetsAt'))}"
                        )
            else:
                lines.append("Model weekly: —")
        updated = state["updated_at"]
        lines.append(
            "Last updated: " + (datetime.fromtimestamp(updated).astimezone().strftime("%Y-%m-%d %H:%M:%S") if updated else "—")
        )
        lines.append("Source: " + state["source"])
        if state["error"]:
            lines.append("Failure: " + state["error"])
        return lines

    def _all_details(self):
        return self._provider_detail_lines("Codex") + [""] + self._provider_detail_lines("Claude")

    def _show_tooltip(self):
        self.tooltip_job = None
        if not self.hovered or self.closed or self.tooltip:
            return
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=BG)
        label = tk.Label(
            tip,
            text="\n".join(self._all_details()),
            justify="left",
            anchor="w",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 10),
            padx=10,
            pady=8,
            relief="solid",
            bd=1,
            wraplength=680,
        )
        label.pack()
        tip.update_idletasks()
        x = min(self.winfo_pointerx() + 14, self.winfo_screenwidth() - tip.winfo_width() - 8)
        y = max(4, self.winfo_pointery() - tip.winfo_height() - 12)
        tip.geometry(f"+{max(4, x)}+{y}")
        self.tooltip = tip

    def _hide_tooltip(self):
        if self.tooltip_job is not None:
            try:
                self.after_cancel(self.tooltip_job)
            except tk.TclError:
                pass
            self.tooltip_job = None
        if self.tooltip is not None:
            try:
                self.tooltip.destroy()
            except tk.TclError:
                pass
            self.tooltip = None

    def pointer_enter(self, _event=None):
        if self.hover_job is not None:
            try:
                self.after_cancel(self.hover_job)
            except tk.TclError:
                pass
            self.hover_job = None
        self.hovered = True
        self.draw_bar()
        if self.tooltip_job is None and self.tooltip is None:
            self.tooltip_job = self.after(650, self._show_tooltip)

    def pointer_leave(self, _event=None):
        if self.hover_job is not None:
            try:
                self.after_cancel(self.hover_job)
            except tk.TclError:
                pass
        self.hover_job = self.after(100, self._finish_pointer_leave)

    def _finish_pointer_leave(self):
        self.hover_job = None
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        inside = (
            self.winfo_rootx() <= px < self.winfo_rootx() + self.bar_width
            and self.winfo_rooty() <= py < self.winfo_rooty() + self.bar_height
        )
        if inside:
            return
        self.hovered = False
        self._hide_tooltip()
        self.draw_bar()

    def begin_interaction(self, event):
        self._hide_tooltip()
        self.drag_origin = (event.x_root, event.y_root, self.winfo_x())
        self.drag_start_left = primary_start_button_left()
        self.drag_moved = False

    def move_interaction(self, event):
        if self.drag_origin is None:
            return
        origin_x, origin_y, window_x = self.drag_origin
        dx = event.x_root - origin_x
        dy = event.y_root - origin_y
        if abs(dx) > 4 or abs(dy) > 4:
            self.drag_moved = True
        if self.drag_moved:
            new_x = window_x + dx
            self.bar_width = width_before_start_button(new_x, self.drag_start_left)
            self.column_edges, self.refresh_width = column_layout(self.bar_width)
            self.canvas.configure(width=self.bar_width)
            self.geometry(f"{self.bar_width}x{self.bar_height}+{new_x}+{self.taskbar_y}")
            self.draw_bar()

    def end_interaction(self, event):
        if self.drag_origin is None:
            return
        was_dragged = self.drag_moved
        self.drag_origin = None
        if was_dragged:
            self.settings.update(x=self.winfo_x(), y=self.taskbar_y)
            save_settings(self.settings)
        elif event.x >= self.bar_width - self.refresh_width:
            self.refresh()
        else:
            self.show_menu(event.x_root, event.y_root)

    def open_menu_event(self, event):
        self._hide_tooltip()
        self.show_menu(event.x_root, event.y_root)

    def show_menu(self, x, y):
        menu = tk.Menu(self, tearoff=False, bg=BG, fg=FG, activebackground="#26374d", activeforeground=FG)
        menu.add_command(label="Codex details", state="disabled")
        for line in self._provider_detail_lines("Codex")[1:]:
            menu.add_command(label="  " + line, state="disabled")
        menu.add_separator()
        menu.add_command(label="Claude details", state="disabled")
        for line in self._provider_detail_lines("Claude")[1:]:
            menu.add_command(label="  " + line, state="disabled")
        menu.add_separator()
        menu.add_command(label="Refresh now", command=self.refresh)
        menu.add_command(label="Restore default position", command=self.restore_default_position)
        menu.add_checkbutton(
            label="Start with Windows",
            variable=self.startup_var,
            command=self.toggle_startup,
        )
        menu.add_separator()
        menu.add_command(label="Exit", command=self.close)
        self.menu = menu
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def restore_default_position(self):
        self.settings.update(x=DEFAULT_X, y=self.taskbar_y)
        self.start_button_left = primary_start_button_left()
        self.bar_width = width_before_start_button(DEFAULT_X, self.start_button_left)
        self.column_edges, self.refresh_width = column_layout(self.bar_width)
        self.canvas.configure(width=self.bar_width)
        self.geometry(f"{self.bar_width}x{self.bar_height}+{DEFAULT_X}+{self.taskbar_y}")
        self.draw_bar()
        save_settings(self.settings)

    def toggle_startup(self):
        path = _startup_shortcut_path()
        try:
            if self.startup_var.get():
                _create_startup_shortcut()
            elif path.exists():
                path.unlink()
            self.last_action_error = None
        except Exception as error:
            self.startup_var.set(path.is_file())
            self.last_action_error = "Startup shortcut update failed"
            log_exception("Startup shortcut update failed", error)
        if self.hovered:
            self._hide_tooltip()
            self.tooltip_job = self.after(100, self._show_tooltip)

    def _start_provider_read(self, name, function):
        if self.fetching[name] or self.closed:
            return
        self.fetching[name] = True
        self.providers[name]["state"] = "refreshing"
        self.providers[name]["error"] = None
        self.draw_bar()
        threading.Thread(
            target=self._provider_worker,
            args=(name, function),
            name=f"quota-read-{name.lower()}",
            daemon=True,
        ).start()

    def refresh(self):
        if self.closed:
            return
        self.last_action_error = None
        self._start_provider_read("Codex", self._read_codex)
        self._start_provider_read("Claude", self._read_claude)

    def _read_codex(self):
        try:
            with self.codex_lock:
                if not self.server_started:
                    LOGGER.info("starting local Codex app-server")
                    self.server.start()
                    self.server_started = True
                    LOGGER.info("local Codex app-server initialized")
                response = self.server.read_limits()
        except Exception:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server_started = False
            raise
        parsed = parse_quota_payload(response)
        return _codex_snapshot(parsed)

    def _read_claude(self):
        parsed = self.claude.read_usage()
        return _claude_snapshot(parsed)

    def _provider_worker(self, name, function):
        try:
            snapshot = function()
        except Exception as error:
            log_exception(f"{name} quota refresh failed", error)
            safe_error = _safe_failure(name, error)
            self._post_provider_result(name, None, safe_error)
        else:
            self._post_provider_result(name, snapshot, None)

    def _post_provider_result(self, name, snapshot, error):
        try:
            self.after(0, self._provider_result, name, snapshot, error)
        except (tk.TclError, RuntimeError):
            pass

    def _provider_result(self, name, snapshot, error):
        if self.closed:
            return
        self.fetching[name] = False
        state = self.providers[name]
        if error is None:
            state["snapshot"] = snapshot
            state["updated_at"] = time.time()
            state["error"] = None
            state["state"] = "live"
        else:
            state["error"] = error
            state["state"] = "offline"
            if name == "Codex" and self.server_started:
                try:
                    if not self.server.proc or self.server.proc.poll() is not None:
                        self.server_started = False
                except Exception:
                    self.server_started = False
        self.draw_bar()
        self.schedule_reset_refresh()

    def auto_refresh(self):
        if not self.closed:
            self.refresh()
            self.after(REFRESH_INTERVAL_MS, self.auto_refresh)

    def check_wake(self):
        if self.closed:
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        wall_gap = now_wall - self.last_wake_wall
        mono_gap = now_mono - self.last_wake_mono
        self.last_wake_wall = now_wall
        self.last_wake_mono = now_mono
        if wall_gap > WAKE_CHECK_INTERVAL_MS / 1000 * 2 or mono_gap > WAKE_CHECK_INTERVAL_MS / 1000 * 2:
            LOGGER.info("system resume detected; refreshing provider quotas")
            self.refresh()
        self.after(WAKE_CHECK_INTERVAL_MS, self.check_wake)

    def schedule_reset_refresh(self):
        if getattr(self, "reset_job", None) is not None:
            try:
                self.after_cancel(self.reset_job)
            except tk.TclError:
                pass
            self.reset_job = None
        resets = []
        for provider in self.providers.values():
            snapshot = provider["snapshot"] or {}
            for key in ("primary", "weekly"):
                window = snapshot.get(key) or {}
                if window.get("resetsAt") is not None:
                    resets.append(window["resetsAt"])
            if snapshot.get("resetAt") is not None:
                resets.append(snapshot["resetAt"])
            for model in snapshot.get("models", {}).values():
                if model.get("resetsAt") is not None:
                    resets.append(model["resetsAt"])
        next_reset = earliest_future_expiry(resets)
        if next_reset is None:
            return
        delay = max(1000, int((next_reset - time.time()) * 1000))
        self.reset_job = self.after(min(delay, 2_147_000_000), self._reset_reached)

    def _reset_reached(self):
        self.reset_job = None
        self.refresh()

    def ensure_taskbar_alignment(self):
        if self.closed:
            return
        try:
            new_height, new_taskbar_y = primary_taskbar_geometry()
            current_x = self.winfo_x()
            new_start_left = primary_start_button_left()
            new_width = width_before_start_button(current_x, new_start_left)
            if new_start_left is None and not self.start_boundary_unavailable:
                LOGGER.warning("Start button boundary unavailable; overlay width minimized")
            elif new_start_left is not None and self.start_boundary_unavailable:
                LOGGER.info("Start button boundary detected; overlay width restored")
            self.start_boundary_unavailable = new_start_left is None
            if (
                new_height != self.bar_height
                or new_taskbar_y != self.taskbar_y
                or new_width != self.bar_width
            ):
                self.bar_height = new_height
                self.taskbar_y = new_taskbar_y
                self.start_button_left = new_start_left
                self.bar_width = new_width
                self.column_edges, self.refresh_width = column_layout(self.bar_width)
                self.geometry(f"{self.bar_width}x{self.bar_height}+{current_x}+{self.taskbar_y}")
                self.canvas.configure(width=self.bar_width, height=self.bar_height)
                self.settings.update(x=current_x, y=self.taskbar_y)
                save_settings(self.settings)
                self.draw_bar()
        except tk.TclError as error:
            log_exception("taskbar alignment check failed", error)
        self.after(TASKBAR_CHECK_INTERVAL_MS, self.ensure_taskbar_alignment)

    def check_fullscreen_state(self):
        if self.closed:
            return
        try:
            fullscreen = primary_screen_has_fullscreen_foreground()
            if fullscreen and not self.hidden_for_fullscreen:
                LOGGER.info("overlay hidden after full-screen foreground detection")
                self.withdraw()
                self.hidden_for_fullscreen = True
            elif not fullscreen and self.hidden_for_fullscreen:
                LOGGER.info("overlay restored after full-screen foreground ended")
                self.deiconify()
                self.attributes("-topmost", True)
                self.lift()
                self.hidden_for_fullscreen = False
        except tk.TclError as error:
            log_exception("fullscreen visibility check failed", error)
        self.after(WINDOW_CHECK_INTERVAL_MS, self.check_fullscreen_state)

    def close(self):
        if self.closed:
            return
        LOGGER.info("window close handler invoked; app-server-started=%s", self.server_started)
        self.closed = True
        self.settings.update(x=self.winfo_x(), y=self.taskbar_y)
        save_settings(self.settings)
        self._hide_tooltip()
        try:
            self.server.stop()
        except Exception as error:
            log_exception("stopping local Codex app-server failed", error)
        self.destroy()


def _count_text(value):
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "—"


def run():
    configure_logging()
    LOGGER.info("overlay run requested")
    app = NarrowBar()
    LOGGER.info("overlay mainloop starting")
    try:
        app.mainloop()
    finally:
        LOGGER.info("overlay mainloop ended; closed=%s", app.closed)


if __name__ == "__main__":
    run()
