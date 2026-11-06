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

# Capsule: the smallest readable form (name | 5h % | weekly %); details stay in the tooltip.
CAPSULE_NAME_WIDTH = 72
CAPSULE_PRIMARY_WIDTH = 68
CAPSULE_WEEKLY_WIDTH = 68
CAPSULE_MAX_WIDTH = CAPSULE_NAME_WIDTH + CAPSULE_PRIMARY_WIDTH + CAPSULE_WEEKLY_WIDTH

# Column order after the name column:
#   primary, weekly, third, mic battery, desktop 1, desktop 2, desktop 3
# Tiers are listed from richest to leanest. A tier is used only when the bar is at least
# as wide as the sum of its preferred column widths, so text is never squeezed illegible;
# extra width inside a tier is shared proportionally. Hidden columns get zero width.
_DESKTOP = DESKTOP_COLUMN_WIDTH
LAYOUT_TIERS = (
    ("full", (PRIMARY_COLUMN_WIDTH, WEEKLY_COLUMN_WIDTH, THIRD_COLUMN_WIDTH, MIC_BATTERY_COLUMN_WIDTH, _DESKTOP, _DESKTOP, _DESKTOP)),
    ("no_desktops", (PRIMARY_COLUMN_WIDTH, WEEKLY_COLUMN_WIDTH, THIRD_COLUMN_WIDTH, MIC_BATTERY_COLUMN_WIDTH, 0, 0, 0)),
    ("no_mic", (PRIMARY_COLUMN_WIDTH, WEEKLY_COLUMN_WIDTH, THIRD_COLUMN_WIDTH, 0, 0, 0, 0)),
    ("no_third", (PRIMARY_COLUMN_WIDTH, WEEKLY_COLUMN_WIDTH, 0, 0, 0, 0, 0)),
)
CAPSULE = "capsule"
COMPACT_BELOW = NAME_COLUMN_MAX + sum(LAYOUT_TIERS[-1][1])  # narrower than this -> capsule


def _clamp_width(width):
    return max(1, min(MAX_PREFERRED_BAR_WIDTH, int(width)))


def layout_mode(width):
    """Name of the richest tier that fits in width (or 'capsule')."""
    width = _clamp_width(width)
    for name, preferred in LAYOUT_TIERS:
        if width >= NAME_COLUMN_MAX + sum(preferred):
            return name
    return CAPSULE


def _share(width, preferred):
    """Split width across the non-zero preferred widths; zero columns stay zero."""
    total = sum(preferred)
    columns = [width * value // total for value in preferred]
    recipients = [index for index, value in enumerate(preferred) if value]
    for position in range(width - sum(columns)):
        columns[recipients[position % len(recipients)]] += 1
    return columns




def width_before_start_button(x, start_left=None, gap=8):
    """Cap the strip at the live Start button boundary, leaving a small gap.

    When even the leanest tier does not fit, the bar becomes a capsule and is
    capped at its natural width instead of stretching over the free space.
    """
    if start_left is None:
        start_left = primary_start_button_left()
    if start_left is None:
        screen_width, _screen_height = primary_screen_size()
        start_left = screen_width
    available = min(MAX_PREFERRED_BAR_WIDTH, int(start_left - int(x) - gap))
    if available < COMPACT_BELOW:
        available = min(available, CAPSULE_MAX_WIDTH)
    return max(1, available)


def column_layout(width):
    """Return 9 cumulative edges: 0, name, primary, weekly, third, mic, desktop 1-3.

    Columns hidden by the current tier have zero width (equal neighbouring edges).
    """
    width = _clamp_width(width)
    mode = layout_mode(width)
    if mode == CAPSULE:
        name, primary, weekly = _share(width, (CAPSULE_NAME_WIDTH, CAPSULE_PRIMARY_WIDTH, CAPSULE_WEEKLY_WIDTH))
        columns = [primary, weekly, 0, 0, 0, 0, 0]
    else:
        preferred = dict(LAYOUT_TIERS)[mode]
        name = NAME_COLUMN_MAX
        columns = _share(width - name, preferred)

    edges = [0, name]
    for column_width in columns:
        edges.append(edges[-1] + column_width)
    return tuple(edges)
