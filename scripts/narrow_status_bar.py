"""Small per-user Codex quota bar for the Windows fork."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import time
from ctypes import Structure, byref, c_long, windll
from pathlib import Path

from api.codex_transport_api import AppServer
from api.quota_format_api import earliest_future_expiry, local_time_date
from api.quota_parse_api import parse_quota_payload


WIDTH, HEIGHT = 613, 46
BG, FG, MUTED, ACCENT, ERROR = "#0b1220", "#e5e7eb", "#9ca3af", "#60a5fa", "#f87171"
QUOTA_GREEN, QUOTA_YELLOW, QUOTA_RED = "#4ade80", "#facc15", "#f87171"
RED_SHORTFALL_PERCENT = 20
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "narrow-status-bar.json"
DEFAULT_X, DEFAULT_Y = -4, -48


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
    return max(40, bottom_height or top_height or HEIGHT)


def primary_taskbar_geometry():
    """Return (height, y) for the taskbar overlay on the primary screen."""
    _left, _top, _right, work_bottom = primary_work_area()
    height = primary_taskbar_height()
    return height, work_bottom


def load_settings():
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return {"x": int(value["x"]), "y": int(value["y"]), "compact": bool(value.get("compact", False))}
    except (OSError, ValueError, KeyError, TypeError):
        left, top, right, bottom = primary_work_area()
        return {"x": DEFAULT_X, "y": DEFAULT_Y, "compact": False}


def save_settings(value):
    try:
        SETTINGS_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def time_text(value):
    return local_time_date(value) if value is not None else "--"


def quota_color(window):
    """Color quota by comparing remaining quota with remaining window time."""
    if not isinstance(window, dict):
        return QUOTA_GREEN
    used = window.get("usedPercent")
    duration = window.get("windowDurationMins")
    reset_at = earliest_future_expiry(window.get("resetsAt"))
    if not isinstance(used, (int, float)) or not isinstance(duration, int) or not reset_at:
        return QUOTA_GREEN
    remaining_quota = max(0.0, min(100.0, 100.0 - float(used)))
    remaining_time = max(0.0, reset_at - time.time())
    remaining_time_percent = min(100.0, remaining_time / (duration * 60.0) * 100.0)
    delta = remaining_quota - remaining_time_percent
    if delta <= -RED_SHORTFALL_PERCENT:
        return QUOTA_RED
    if delta < 0:
        return QUOTA_YELLOW
    return QUOTA_GREEN


class NarrowBar(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.bar_height, self.taskbar_y = primary_taskbar_geometry()
        self.title("Codex 用量")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        self.configure(bg=BG)
        self.apply_position()
        self.settings.update(y=self.taskbar_y)
        save_settings(self.settings)
        self.resizable(False, False)
        self.drag_origin = None
        self.hover_job = None
        self.server = AppServer(queue.Queue(), client_version="1.1.0")
        self.server_started = False
        self.refreshing = False
        self.closed = False

        self.line1 = tk.Frame(self, bg=BG)
        self.line1.place(x=12, y=1, width=WIDTH - 50, height=23)
        line_font = ("Consolas", 12)
        self.line1_status = tk.Label(self.line1, text="Codex Connecting…", bg=BG, fg=FG, anchor="w", font=line_font)
        self.line1_primary = tk.Label(self.line1, text="", bg=BG, fg=QUOTA_GREEN, anchor="w", font=line_font)
        self.line1_separator = tk.Label(self.line1, text="", bg=BG, fg=FG, anchor="w", font=line_font)
        self.line1_weekly = tk.Label(self.line1, text="", bg=BG, fg=QUOTA_GREEN, anchor="w", font=line_font)
        self.line1_status.pack(side="left")
        self.line1_primary.pack(side="left", padx=(12, 0))
        self.line1_separator.pack(side="left", padx=(12, 0))
        self.line1_weekly.pack(side="left", padx=(4, 0))
        self.line2 = tk.Label(self, text="Loading quota…", bg=BG, fg=MUTED, anchor="w", font=("Consolas", 12))
        self.line2.place(x=12, y=max(22, self.bar_height - 22), width=WIDTH - 116, height=18)
        self.refresh_button = tk.Button(self, text="↻", command=self.refresh, bg=BG, fg=ACCENT, activebackground="#16233a", activeforeground=FG, bd=0, highlightthickness=0, font=("Segoe UI", 11, "bold"))
        self.refresh_button.place_forget()
        self.compact_button = tk.Button(self, text="↕", command=self.toggle_compact, bg=BG, fg=MUTED, activebackground="#16233a", activeforeground=FG, bd=0, highlightthickness=0, font=("Segoe UI", 10))
        self.compact_button.place_forget()
        self.close_button = tk.Button(self, text="×", command=self.close, bg=BG, fg=MUTED, activebackground="#3a1720", activeforeground=ERROR, bd=0, highlightthickness=0, font=("Segoe UI", 14))
        self.close_button.place(x=WIDTH - 30, y=max(0, self.bar_height - 32), width=28, height=32)
        for widget in (self, self.line1, self.line1_status, self.line1_primary, self.line1_separator, self.line1_weekly, self.line2):
            widget.bind("<ButtonPress-1>", self.begin_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.end_drag)
        for widget in (self, self.line1, self.line1_status, self.line1_primary, self.line1_separator, self.line1_weekly, self.line2, self.refresh_button, self.compact_button, self.close_button):
            widget.bind("<Enter>", self.pointer_enter, add="+")
            widget.bind("<Leave>", self.pointer_leave, add="+")
        # Start with the same transparent background used when the pointer leaves.
        # Text remains opaque because transparentcolor only removes BG pixels.
        try:
            self.attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.refresh)
        self.after(5000, self.auto_refresh)
        self.after(60000, self.ensure_visible)

    def ensure_visible(self):
        if self.closed:
            return
        try:
            new_height, new_taskbar_y = primary_taskbar_geometry()
            if new_height != self.bar_height or new_taskbar_y != self.taskbar_y:
                current_x = self.winfo_x()
                self.bar_height = new_height
                self.taskbar_y = new_taskbar_y
                self.line2.place_configure(y=max(22, self.bar_height - 22))
                self.close_button.place_configure(y=max(0, self.bar_height - 32))
                self.geometry(f"{WIDTH}x{self.bar_height}+{current_x}+{self.taskbar_y}")
                self.settings.update(x=current_x, y=self.taskbar_y)
                save_settings(self.settings)
            self.deiconify()
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            return
        self.after(60000, self.ensure_visible)

    def apply_position(self):
        x = int(self.settings["x"])
        self.geometry(f"{WIDTH}x{self.bar_height}+{x}+{self.taskbar_y}")

    def auto_refresh(self):
        if not self.closed:
            self.refresh()
            self.after(5000, self.auto_refresh)

    def begin_drag(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())

    def drag(self, event):
        if self.drag_origin is None:
            return
        ox, oy, x, y = self.drag_origin
        self.geometry(f"+{x + event.x_root - ox}+{y + event.y_root - oy}")

    def end_drag(self, _event):
        self.settings.update(x=self.winfo_x(), y=self.winfo_y())
        save_settings(self.settings)
        self.drag_origin = None

    def pointer_enter(self, _event=None):
        if self.hover_job is not None:
            self.after_cancel(self.hover_job)
            self.hover_job = None
        self.attributes("-alpha", 0.94)
        try:
            self.attributes("-transparentcolor", "")
        except tk.TclError:
            pass

    def pointer_leave(self, _event=None):
        if self.hover_job is not None:
            self.after_cancel(self.hover_job)
        self.hover_job = self.after(80, self.apply_unhovered_alpha)

    def apply_unhovered_alpha(self):
        self.hover_job = None
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        inside = self.winfo_rootx() <= px < self.winfo_rootx() + self.winfo_width() and self.winfo_rooty() <= py < self.winfo_rooty() + self.winfo_height()
        if inside:
            self.attributes("-alpha", 0.94)
            try:
                self.attributes("-transparentcolor", "")
            except tk.TclError:
                pass
        else:
            self.attributes("-alpha", 0.94)
            try:
                self.attributes("-transparentcolor", BG)
            except tk.TclError:
                pass

    def toggle_compact(self):
        self.settings["compact"] = not self.settings.get("compact", False)
        self.line2.configure(state="normal")
        if self.settings["compact"]:
            self.line2.place_forget()
            self.geometry(f"{WIDTH}x{26}+{self.winfo_x()}+{self.winfo_y()}")
        else:
            self.line2.place(x=12, y=max(22, self.bar_height - 22), width=WIDTH - 116, height=18)
            self.geometry(f"{WIDTH}x{self.bar_height}+{self.winfo_x()}+{self.winfo_y()}")
        save_settings(self.settings)

    def refresh(self):
        if self.closed or self.refreshing:
            return
        self.refreshing = True
        threading.Thread(target=self._read_quota, daemon=True).start()

    def _read_quota(self):
        try:
            if not self.server_started:
                self.server.start()
                self.server_started = True
            parsed = parse_quota_payload(self.server.read_limits())
            self.after(0, self.deliver_quota, parsed)
        except Exception:
            self.after(0, self.show_offline)

    def deliver_quota(self, parsed):
        try:
            self.show_quota(parsed)
        except Exception:
            self.show_offline()

    def show_offline(self):
        self.refreshing = False
        self.line1_status.configure(text="Codex Offline / Quota unavailable", fg=ERROR)
        self.line1_primary.configure(text="", fg=QUOTA_GREEN)
        self.line1_separator.configure(text="")
        self.line1_weekly.configure(text="", fg=QUOTA_GREEN)
        self.line2.configure(text="Check that Codex is available; estimates are not shown")

    def show_quota(self, data):
        self.refreshing = False
        limits = data.get("rateLimits", {})
        primary = limits.get("primary", {})
        weekly = limits.get("secondary", {})
        primary_left = 100 - float(primary.get("usedPercent", 100)) if primary else None
        weekly_left = 100 - float(weekly.get("usedPercent", 100)) if weekly else None
        def pct(value): return "--" if value is None else f"{max(0, min(100, round(value)))}%"
        p = f"5h {pct(primary_left)} | {time_text(primary.get('resetsAt'))}"
        w = f"WEEK {pct(weekly_left)} | {time_text(weekly.get('resetsAt'))}"
        credits = data.get("rateLimitResetCredits", {}).get("availableCount", "--")
        self.line1_status.configure(text="Codex LIVE", fg=FG)
        self.line1_primary.configure(text=p, fg=quota_color(primary))
        self.line1_separator.configure(text="●", fg=FG)
        self.line1_weekly.configure(text=w, fg=quota_color(weekly))
        self.line2.configure(text=f"Reset credits {credits} ● Auto refresh 5s")

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.settings.update(x=self.winfo_x(), y=self.winfo_y())
        save_settings(self.settings)
        try:
            self.server.stop()
        except Exception:
            pass
        self.destroy()


def run():
    app = NarrowBar()
    app.mainloop()


if __name__ == "__main__":
    run()
