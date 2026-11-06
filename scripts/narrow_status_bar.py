"""Two-provider per-user quota bar for the Windows fork."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont

from api.claude_usage_api import ClaudeUsageClient
from api.debounce_api import Debouncer
from api.bluetooth_battery_api import read_connected_dji_mic_mini_2_battery
from api.codex_transport_api import AppServer
from api.narrow_bar_logging_api import LOGGER, configure_logging, log_exception
from api.quota_format_api import earliest_future_expiry, reset_display_date
from api.quota_parse_api import parse_quota_payload
from api.bar_layout_api import (
    CAPSULE,
    MIN_PREFERRED_BAR_WIDTH,
    column_layout,
    layout_mode,
    width_before_start_button,
)
from api.provider_snapshot_api import (
    claude_snapshot as _claude_snapshot,
    codex_snapshot as _codex_snapshot,
    count_text as _count_text,
    percent as _percent,
    reset as _reset,
    safe_failure as _safe_failure,
)
from api.win_shell_api import (
    TaskbarWatcher,
    WinEventHook,
    primary_screen_size,
    primary_start_button_left,
    primary_taskbar_geometry,
    send_control_number,
    set_window_topmost,
    toplevel_hwnd,
    window_needs_topmost,
)
from api.virtual_desktop_api import (
    DESKTOP_NAMES,
    RegistryWatcher,
    read_active_virtual_desktop_name,
)


BG = "#101929"
FG = "#EDF1F9"
MUTED = "#97B1CC"
BORDER = "#4A5A66"
DESKTOP_INACTIVE_BG = "#34404B"
DESKTOP_TEXT = "#FFFFFF"
TRANSPARENT = "#010203"
CODEX_NAME = "#C61862"
DESKTOP_ACTIVE_BG = CODEX_NAME
CODEX_THIRD = "#205B4E"
CLAUDE_NAME = "#B4532A"
CLAUDE_THIRD = "#2E4E7A"
DOT_LIVE = "#35D1A8"
DOT_REFRESHING = "#61C6D8"
DOT_OFFLINE = "#EFB86B"
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "narrow-status-bar.json"
DEFAULT_X, DEFAULT_Y = -4, -48
REFRESH_INTERVAL_MS = 5 * 60 * 1000
CLAUDE_REFRESH_INTERVAL_S = 20 * 60
WAKE_CHECK_INTERVAL_MS = 30 * 1000
TASKBAR_CHECK_INTERVAL_MS = 60 * 1000  # backstop while the taskbar watcher is active
TASKBAR_POLL_FALLBACK_MS = 5 * 1000  # used only when the taskbar watcher cannot start
GEOMETRY_EVENT_DEBOUNCE_MS = 400  # Explorer emits bursts of events while it re-lays out
GEOMETRY_CONFIRM_MS = 1500  # one more pass after things settle
WINDOW_CHECK_INTERVAL_MS = 15 * 1000  # backstop; WinEvent hook triggers the normal checks
TOPMOST_EVENT_DEBOUNCE_MS = 150
DESKTOP_POLL_FALLBACK_MS = 1000  # only used when the registry watcher cannot start
DESKTOP_SAFETY_POLL_MS = 30 * 1000  # backstop while the watcher is active
MIC_BATTERY_REFRESH_INTERVAL_MS = 5 * 60 * 1000



def fit_text(text, font, width):
    """Return a single-line label that fits its cell without crossing a divider."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    suffix = "…"
    while text and font.measure(text + suffix) > width:
        text = text[:-1]
    return text + suffix if text else (suffix if font.measure(suffix) <= width else "")


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
        self.screen_width, self.screen_height = primary_screen_size()
        initial_x = max(-4, min(self.settings["x"], self.screen_width - 1))
        initial_width = width_before_start_button(initial_x, self.start_button_left)
        if initial_width < MIN_PREFERRED_BAR_WIDTH and initial_x > -4:
            left_width = width_before_start_button(-4, self.start_button_left)
            if left_width > initial_width:
                initial_x = -4
        self.settings["x"] = initial_x
        self.bar_width = width_before_start_button(
            initial_x, start_left=self.start_button_left
        )
        self.column_edges = column_layout(self.bar_width)
        self.start_boundary_unavailable = self.start_button_left is None
        self.layout_signature = (
            self.screen_width,
            self.screen_height,
            self.bar_height,
            self.taskbar_y,
            self.start_button_left,
        )
        self.taskbar_alignment_job = None
        self.desktop_check_job = None
        self.mic_battery_job = None
        if self.start_boundary_unavailable:
            LOGGER.warning("Start button boundary unavailable; using primary screen edge as width limit")
        self.title("Codex and Claude Usage")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=TRANSPARENT)
        self.settings["y"] = self.taskbar_y
        self.geometry(f"{self.bar_width}x{self.bar_height}+{self.settings['x']}+{self.taskbar_y}")
        self.resizable(False, False)

        self.closed = False
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
        self.mic_battery_fetching = False
        self.mic_battery = {
            "connected": False,
            "label": "MIC",
            "level": None,
            "selected": None,
            "devices": [],
            "updated_at": None,
            "error": None,
        }
        self.active_desktop_name = read_active_virtual_desktop_name()
        LOGGER.info(
            "active virtual desktop indicator initialized: %s",
            self.active_desktop_name or "unavailable",
        )
        self.desktop_watcher = RegistryWatcher(self._desktop_signal)
        self.desktop_poll_ms = (
            DESKTOP_SAFETY_POLL_MS if self.desktop_watcher.start() else DESKTOP_POLL_FALLBACK_MS
        )
        LOGGER.info("virtual desktop change watcher active=%s", self.desktop_watcher.active)

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
        self.font_desktop = tkfont.Font(self, family="Segoe UI", size=-17)
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

        self.geometry_debounce = Debouncer(self.after, self.after_cancel, GEOMETRY_EVENT_DEBOUNCE_MS, self._geometry_settled)
        self.geometry_confirm = Debouncer(self.after, self.after_cancel, GEOMETRY_CONFIRM_MS, self.apply_taskbar_alignment)
        self.geometry_watcher = TaskbarWatcher(self._geometry_signal)
        geometry_events = self.geometry_watcher.start()
        self.taskbar_poll_ms = TASKBAR_CHECK_INTERVAL_MS if geometry_events else TASKBAR_POLL_FALLBACK_MS
        LOGGER.info("taskbar geometry watcher active=%s", geometry_events)

        self.after(100, self.refresh)
        self.after(REFRESH_INTERVAL_MS, self.auto_refresh)
        self.after(WAKE_CHECK_INTERVAL_MS, self.check_wake)
        self.taskbar_alignment_job = self.after(self.taskbar_poll_ms, self.ensure_taskbar_alignment)
        self.desktop_check_job = self.after(self.desktop_poll_ms, self.check_active_desktop)
        self.mic_battery_job = self.after(250, self.refresh_mic_battery)
        self.after(WINDOW_CHECK_INTERVAL_MS, self.check_window_visibility)
        self.topmost_job = None
        self.topmost_hook = WinEventHook(self._topmost_signal)
        LOGGER.info("foreground WinEvent hook active=%s", self.topmost_hook.start())

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
                if edges[3] > edges[2]:
                    c.create_rectangle(
                        edges[2],
                        y1 + (1 if row == 0 else 0),
                        edges[3],
                        y2,
                        fill=CODEX_THIRD if row == 0 else CLAUDE_THIRD,
                        outline="",
                    )

        for index, name in enumerate(DESKTOP_NAMES):
            left, right = edges[5 + index], edges[6 + index]
            if right <= left:
                continue
            c.create_rectangle(
                left + 1,
                1,
                right - 1,
                self.bar_height - 1,
                fill=DESKTOP_ACTIVE_BG if name == self.active_desktop_name else DESKTOP_INACTIVE_BG,
                outline="",
            )

        mic_left, mic_right = edges[4], edges[5]
        if mic_right > mic_left:
            c.create_rectangle(
                mic_left + 1,
                1,
                mic_right - 1,
                self.bar_height - 1,
                fill=BG,
                outline="",
            )

        if self.background_visible:
            # Hidden columns collapse several edges onto one x; draw each divider once,
            # and keep the last one inside the window so it closes the right side.
            for x in sorted(set(edges[1:-1])):
                x = min(x, width - 1)
                c.create_line(x, 0, x, self.bar_height, fill=BORDER, width=1)
            c.create_line(0, mid, edges[5], mid, fill=BORDER, width=1)
            self._draw_outer_frame_to_data_end(edges[5])

        self._draw_provider("Codex", 0, row_centres[0])
        self._draw_provider("Claude", 1, row_centres[1])
        self._draw_desktop_indicators(edges)
        self._draw_mic_battery(edges)

    def _draw_desktop_indicators(self, edges):
        center_y = self.bar_height / 2
        for index, name in enumerate(DESKTOP_NAMES):
            left, right = edges[5 + index], edges[6 + index]
            if right <= left:
                continue
            center_x = (left + right) / 2
            self.canvas.create_text(
                center_x,
                center_y,
                anchor="center",
                text=name,
                fill=DESKTOP_TEXT,
                font=self.font_desktop,
            )

    def _draw_mic_battery(self, edges):
        left, right = edges[4], edges[5]
        if right <= left:
            return
        battery = self.mic_battery
        devices = {item["label"]: item for item in battery["devices"]}
        self._draw_mic_battery_line(left, right, self.bar_height / 4, "GPT", devices.get("GPT"))
        self._draw_mic_battery_line(left, right, self.bar_height * 3 / 4, "CC", devices.get("CC"))

    def _draw_mic_battery_line(self, left, right, y, label, device):
        level = device["level"] if device else None
        online = bool(device and device.get("connected"))
        value = f"{level}%" if isinstance(level, int) and not isinstance(level, bool) else "--"
        color = FG if online else "#8A93A2"
        segments = [(f"{label} ", self.font_ui), (value, self.font_numbers)]

        available = max(0, right - left - 8)
        total_width = sum(font.measure(text) for text, font in segments)
        if total_width > available:
            self.canvas.create_text(
                (left + right) / 2,
                y,
                anchor="center",
                text=fit_text(
                    f"{label} {value}",
                    self.font_ui,
                    available,
                ),
                fill=color,
                font=self.font_ui,
            )
            return

        x = (left + right - total_width) / 2
        for text, font in segments:
            self.canvas.create_text(x, y, anchor="w", text=text, fill=color, font=font)
            x += font.measure(text)

    def _draw_outer_frame_to_data_end(self, content_right):
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
        c.create_line(left + radius, top, content_right, top, fill=BORDER, width=1)
        c.create_line(left, top + radius, left, bottom - radius, fill=BORDER, width=1)
        c.create_arc(
            left, bottom - 2 * radius, right, bottom,
            start=180, extent=90, style="arc", outline=BORDER, width=1,
        )
        c.create_line(left + radius, bottom, content_right, bottom, fill=BORDER, width=1)

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
        name_width = max(0, edges[1] - x0)
        dot_x = x0 + min(11, max(4, round(name_width * 0.14)))
        label_x = x0 + min(23, max(14, round(name_width * 0.30)))
        label_width = max(0, edges[1] - label_x - 4)
        self.canvas.create_oval(
            dot_x,
            y - 2.5,
            dot_x + 5,
            y + 2.5,
            fill=dot_color,
            outline="",
        )
        label = name if state["state"] == "live" else f"{name} {state['state'].title()}"
        label = fit_text(label, self.font_bold, label_width)
        self.canvas.create_text(
            label_x,
            y,
            anchor="w",
            text=label,
            fill=FG,
            font=self.font_bold,
        )

        snapshot = state["snapshot"] or {}
        primary = snapshot.get("primary") or {}
        weekly = snapshot.get("weekly") or {}
        if name == "Codex":
            third_segments = [
                (_count_text(snapshot.get("resetCount")), self.font_numbers),
                (" times ", self.font_ui),
                (reset_display_date(snapshot.get("resetAt")), self.font_numbers),
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
        pad = min(10, max(5, round(self.bar_width * 0.01)))
        capsule = layout_mode(self.bar_width) == CAPSULE

        primary_segments = [
            ("5h ", self.font_ui),
            (_percent(primary.get("remainingPercent")), self.font_numbers),
        ]
        weekly_segments = [
            ("W " if capsule else "WEEK ", self.font_ui),
            (_percent(weekly.get("remainingPercent")), self.font_numbers),
        ]
        if not capsule:  # reset times need the room the capsule does not have
            primary_segments += [(" / ", self.font_ui), (_reset(primary.get("resetsAt")), self.font_numbers)]
            weekly_segments += [(" / ", self.font_ui), (_reset(weekly.get("resetsAt")), self.font_numbers)]

        self._draw_segments(edges[1] + pad, y, primary_segments, edges[2] - edges[1] - 2 * pad)
        self._draw_segments(edges[2] + pad, y, weekly_segments, edges[3] - edges[2] - 2 * pad)
        if edges[4] > edges[3]:
            self._draw_segments(edges[3] + pad, y, third_segments, edges[4] - edges[3] - 2 * pad)

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
        return (
            self._provider_detail_lines("Codex")
            + [""]
            + self._provider_detail_lines("Claude")
            + [""]
            + self._mic_battery_detail_lines()
        )

    def _mic_battery_detail_lines(self):
        battery = self.mic_battery
        connected = battery["connected"]
        level = battery["level"] if connected else None
        state = "Unavailable" if battery["error"] else ("Connected" if connected else "Not connected")
        level_text = f"{level}%" if isinstance(level, int) and not isinstance(level, bool) else "—"
        updated = battery["updated_at"]
        devices = {item["label"]: item for item in battery["devices"]}
        displayed = "; ".join(
            f"{label} {devices[label]['level']}%"
            if devices.get(label, {}).get("connected") and isinstance(devices[label].get("level"), int)
            else f"{label} --"
            for label in ("GPT", "CC")
        )
        lowest = (
            f"{battery['selected']} {level_text}"
            if battery["selected"] and level is not None
            else "—"
        )
        lines = [
            f"DJI Mic Mini 2 — {state}",
            f"Displayed: {displayed}",
            f"Lowest connected device: {lowest}",
            "Last checked: " + (datetime.fromtimestamp(updated).astimezone().strftime("%Y-%m-%d %H:%M:%S") if updated else "—"),
            "Source: Windows Bluetooth HFP device battery property",
        ]
        for label in ("GPT", "CC"):
            device = next((item for item in battery["devices"] if item["label"] == label), None)
            device_level = device["level"] if device else None
            device_text = (
                f"{device_level}%"
                if device and device.get("connected") and isinstance(device_level, int)
                else "--"
            )
            lines.append(f"{label}: {device_text}")
        if battery["error"]:
            lines.append("Failure: Windows battery property could not be read")
        return lines

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
            self.column_edges = column_layout(self.bar_width)
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
        else:
            desktop_index = next(
                (
                    index
                    for index in range(len(DESKTOP_NAMES))
                    if self.column_edges[5 + index] <= event.x < self.column_edges[6 + index]
                ),
                None,
            )
            if desktop_index is not None:
                shortcut_number = desktop_index + 1
                try:
                    send_control_number(shortcut_number)
                    LOGGER.info("virtual desktop shortcut sent: Ctrl+%s", shortcut_number)
                except Exception as error:
                    log_exception("virtual desktop shortcut failed", error)
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
        menu.add_command(label="DJI Mic Mini 2", state="disabled")
        for line in self._mic_battery_detail_lines()[1:]:
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
        self.column_edges = column_layout(self.bar_width)
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
        self.last_claude_refresh = time.monotonic()

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
            claude_due = (
                time.monotonic() - getattr(self, "last_claude_refresh", 0) >= CLAUDE_REFRESH_INTERVAL_S - 5
            )
            if claude_due:
                self.refresh()
            else:
                self._start_provider_read("Codex", self._read_codex)
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

    def _desktop_signal(self):
        """Watcher thread wake-up: hop to the Tk thread and re-read the desktop."""
        try:
            self.after(0, self.refresh_active_desktop)
        except (tk.TclError, RuntimeError):
            pass

    def refresh_active_desktop(self):
        if self.closed:
            return
        current = read_active_virtual_desktop_name()
        if current != self.active_desktop_name:
            self.active_desktop_name = current
            LOGGER.info(
                "active virtual desktop indicator changed: %s",
                current or "unavailable",
            )
            self.draw_bar()

    def check_active_desktop(self):
        if self.closed:
            return
        self.refresh_active_desktop()
        self.desktop_check_job = self.after(self.desktop_poll_ms, self.check_active_desktop)

    def refresh_mic_battery(self):
        if self.closed:
            return
        self.mic_battery_job = self.after(
            MIC_BATTERY_REFRESH_INTERVAL_MS,
            self.refresh_mic_battery,
        )
        if self.mic_battery_fetching:
            return
        self.mic_battery_fetching = True
        threading.Thread(
            target=self._mic_battery_worker,
            name="dji-mic-battery-read",
            daemon=True,
        ).start()

    def _mic_battery_worker(self):
        try:
            reading = read_connected_dji_mic_mini_2_battery()
            error = None
        except Exception:
            reading = {
                "connected": False,
                "label": "MIC",
                "level": None,
                "selected": None,
                "devices": [],
            }
            error = "Windows battery property could not be read"
            LOGGER.warning("DJI Mic Mini 2 battery property read failed")
        try:
            self.after(0, self._mic_battery_result, reading, error)
        except (tk.TclError, RuntimeError):
            pass

    def _mic_battery_result(self, reading, error):
        if self.closed:
            return
        self.mic_battery_fetching = False
        self.mic_battery = {
            **reading,
            "updated_at": time.time(),
            "error": error,
        }
        self.draw_bar()

    def _geometry_signal(self):
        """Hook thread: only hand off to the Tk thread, never touch widgets here."""
        try:
            self.after(0, self.geometry_debounce.trigger)
        except (tk.TclError, RuntimeError):
            pass

    def _geometry_settled(self):
        if self.closed:
            return
        self.apply_taskbar_alignment()
        self.geometry_confirm.trigger()

    def ensure_taskbar_alignment(self):
        """Slow backstop: also re-targets the watcher after an Explorer restart."""
        if self.closed:
            return
        self.taskbar_alignment_job = None
        try:
            self.geometry_watcher.retarget()
        except Exception as error:
            log_exception("taskbar watcher retarget failed", error)
        self.taskbar_poll_ms = (
            TASKBAR_CHECK_INTERVAL_MS if self.geometry_watcher.active else TASKBAR_POLL_FALLBACK_MS
        )
        self.apply_taskbar_alignment()
        if not self.closed:
            self.taskbar_alignment_job = self.after(self.taskbar_poll_ms, self.ensure_taskbar_alignment)

    def apply_taskbar_alignment(self):
        if self.closed:
            return
        try:
            new_height, new_taskbar_y = primary_taskbar_geometry()
            new_screen_width, new_screen_height = primary_screen_size()
            current_x = max(-4, min(self.winfo_x(), new_screen_width - 1))
            new_start_left = primary_start_button_left()
            new_width = width_before_start_button(current_x, new_start_left)
            signature = (
                new_screen_width,
                new_screen_height,
                new_height,
                new_taskbar_y,
                new_start_left,
            )
            environment_changed = signature != self.layout_signature
            if (
                environment_changed
                and new_width < MIN_PREFERRED_BAR_WIDTH
                and current_x > -4
            ):
                left_width = width_before_start_button(-4, new_start_left)
                if left_width > new_width:
                    current_x = -4
                    new_width = left_width
            if new_start_left is None and not self.start_boundary_unavailable:
                LOGGER.warning("Start button boundary unavailable; using primary screen edge as width limit")
            elif new_start_left is not None and self.start_boundary_unavailable:
                LOGGER.info("Start button boundary detected; overlay width restored")
            self.start_boundary_unavailable = new_start_left is None
            if (
                environment_changed
                or new_screen_width != self.screen_width
                or new_screen_height != self.screen_height
                or new_height != self.bar_height
                or new_taskbar_y != self.taskbar_y
                or new_width != self.bar_width
                or current_x != self.winfo_x()
            ):
                self.screen_width = new_screen_width
                self.screen_height = new_screen_height
                self.bar_height = new_height
                self.taskbar_y = new_taskbar_y
                self.start_button_left = new_start_left
                self.bar_width = new_width
                self.column_edges = column_layout(self.bar_width)
                self.geometry(f"{self.bar_width}x{self.bar_height}+{current_x}+{self.taskbar_y}")
                self.canvas.configure(width=self.bar_width, height=self.bar_height)
                if (current_x, self.taskbar_y) != (self.settings.get("x"), self.settings.get("y")):
                    self.settings.update(x=current_x, y=self.taskbar_y)
                    save_settings(self.settings)
                self.draw_bar()
                LOGGER.info(
                    "overlay layout recalculated: screen=%sx%s taskbar=%sx%s x=%s start-left=%s width=%s columns=%s",
                    new_screen_width,
                    new_screen_height,
                    new_height,
                    new_taskbar_y,
                    current_x,
                    new_start_left,
                    new_width,
                    self.column_edges,
                )
            self.layout_signature = signature
            self.screen_width = new_screen_width
            self.screen_height = new_screen_height
        except tk.TclError as error:
            log_exception("taskbar alignment check failed", error)

    def reassert_window_topmost(self):
        if self.closed:
            return
        try:
            if self.state() == "withdrawn":
                self.deiconify()
            hwnd = toplevel_hwnd(self.winfo_id())
            if not window_needs_topmost(hwnd):
                return
            try:
                if set_window_topmost(hwnd):
                    LOGGER.info("overlay was covered; topmost restored")
                else:
                    LOGGER.warning("SetWindowPos(HWND_TOPMOST) did not succeed")
            except Exception as error:
                log_exception("SetWindowPos(HWND_TOPMOST) failed", error)
        except tk.TclError as error:
            log_exception("topmost reassertion failed", error)

    def _topmost_signal(self):
        """Hook thread: only hand off to the Tk thread, never touch widgets here."""
        try:
            self.after(0, self._topmost_event)
        except (tk.TclError, RuntimeError):
            pass

    def _topmost_event(self):
        """Foreground/minimise event: re-check shortly, after the shell settles."""
        if self.closed or self.topmost_job is not None:
            return
        self.topmost_job = self.after(TOPMOST_EVENT_DEBOUNCE_MS, self._topmost_event_check)

    def _topmost_event_check(self):
        self.topmost_job = None
        self.reassert_window_topmost()

    def check_window_visibility(self):
        if self.closed:
            return
        self.reassert_window_topmost()
        self.after(WINDOW_CHECK_INTERVAL_MS, self.check_window_visibility)

    def close(self):
        if self.closed:
            return
        LOGGER.info("window close handler invoked; app-server-started=%s", self.server_started)
        self.closed = True
        self.settings.update(x=self.winfo_x(), y=self.taskbar_y)
        save_settings(self.settings)
        if self.taskbar_alignment_job is not None:
            try:
                self.after_cancel(self.taskbar_alignment_job)
            except tk.TclError:
                pass
            self.taskbar_alignment_job = None
        if self.desktop_check_job is not None:
            try:
                self.after_cancel(self.desktop_check_job)
            except tk.TclError:
                pass
            self.desktop_check_job = None
        if self.mic_battery_job is not None:
            try:
                self.after_cancel(self.mic_battery_job)
            except tk.TclError:
                pass
            self.mic_battery_job = None
        self._hide_tooltip()
        self.geometry_debounce.cancel()
        self.geometry_confirm.cancel()
        try:
            self.geometry_watcher.stop()
        except Exception as error:
            log_exception("stopping taskbar watcher failed", error)
        try:
            self.topmost_hook.stop()
        except Exception as error:
            log_exception("removing WinEvent hook failed", error)
        try:
            self.desktop_watcher.stop()
        except Exception as error:
            log_exception("stopping virtual desktop watcher failed", error)
        try:
            self.server.stop()
        except Exception as error:
            log_exception("stopping local Codex app-server failed", error)
        self.destroy()


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
