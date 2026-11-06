"""Pure layout maths for the taskbar overlay (no Tk, no Win32 calls on the pure paths)."""

from __future__ import annotations

from api.win_shell_api import primary_screen_size, primary_start_button_left


PREFERRED_BAR_WIDTH = 792
MAX_PREFERRED_BAR_WIDTH = 900
NAME_COLUMN_MAX = 88
NAME_COLUMN_MIN = 50
PRIMARY_COLUMN_WIDTH = 140
WEEKLY_COLUMN_WIDTH = 170
THIRD_COLUMN_WIDTH = 109
DESKTOP_COLUMN_WIDTH = 72
MIC_BATTERY_COLUMN_WIDTH = 69
MIN_PREFERRED_BAR_WIDTH = 180


def width_before_start_button(x, start_left=None, gap=8):
    """Cap the strip at the live Start button boundary, leaving a small gap."""
    if start_left is None:
        start_left = primary_start_button_left()
    if start_left is None:
        screen_width, _screen_height = primary_screen_size()
        start_left = screen_width
    return max(1, min(MAX_PREFERRED_BAR_WIDTH, int(start_left - int(x) - gap)))


def column_layout(width):
    """Fit quota, desktop and mic battery indicators into the available width."""
    width = max(1, min(MAX_PREFERRED_BAR_WIDTH, int(width)))
    name = min(NAME_COLUMN_MAX, width)
    name_floor = min(NAME_COLUMN_MIN, width)
    name -= min(max(0, name - name_floor), max(0, PREFERRED_BAR_WIDTH - width))
    remaining = max(0, width - name)

    preferred = (
        PRIMARY_COLUMN_WIDTH,
        WEEKLY_COLUMN_WIDTH,
        THIRD_COLUMN_WIDTH,
        MIC_BATTERY_COLUMN_WIDTH,
        DESKTOP_COLUMN_WIDTH,
        DESKTOP_COLUMN_WIDTH,
        DESKTOP_COLUMN_WIDTH,
    )
    preferred_total = sum(preferred)
    columns = [remaining * value // preferred_total for value in preferred]
    for index in range(remaining - sum(columns)):
        columns[index] += 1

    edges = [0, name]
    for column_width in columns:
        edges.append(edges[-1] + column_width)
    return tuple(edges)
