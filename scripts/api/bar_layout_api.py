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
CAPSULE_MIN_WIDTH = CAPSULE_NAME_WIDTH + CAPSULE_PRIMARY_WIDTH + CAPSULE_WEEKLY_WIDTH
# With a little more room the capsule also keeps the mic battery column.
CAPSULE_MIC_WIDTH = CAPSULE_MIN_WIDTH + MIC_BATTERY_COLUMN_WIDTH

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
CAPSULE_MIC = "capsule_mic"
COMPACT_MODES = (CAPSULE, CAPSULE_MIC)
COMPACT_BELOW = NAME_COLUMN_MAX + sum(LAYOUT_TIERS[-1][1])  # narrower than this -> capsule


def _clamp_width(width):
    return max(1, min(MAX_PREFERRED_BAR_WIDTH, int(width)))


def layout_mode(width):
    """Name of the richest tier that fits in width (or 'capsule')."""
    width = _clamp_width(width)
    for name, preferred in LAYOUT_TIERS:
        if width >= NAME_COLUMN_MAX + sum(preferred):
            return name
    return CAPSULE_MIC if width >= CAPSULE_MIC_WIDTH else CAPSULE


def _share(width, preferred):
    """Split width across the non-zero preferred widths; zero columns stay zero."""
    total = sum(preferred)
    columns = [width * value // total for value in preferred]
    recipients = [index for index, value in enumerate(preferred) if value]
    for position in range(width - sum(columns)):
        columns[recipients[position % len(recipients)]] += 1
    return columns




START_RETRY_INTERVAL_MS = 500
START_RETRY_LIMIT = 20  # ~10 s; the slow backstop poll continues after that


class StartBoundary:
    """Remember the last good Start-button edge.

    Explorer removes the taskbar's Start window for a second or two whenever the
    Start menu is open, so "not found" usually means "temporarily hidden", not
    "gone". Widening the bar to the screen edge at that moment covers the real
    button, so a missing reading keeps the previous value and asks for a quick retry.
    """

    def __init__(self, initial=None):
        self.held = initial
        self.misses = 0

    def update(self, raw):
        """Feed a fresh reading (or None); returns the edge to lay out against."""
        if raw is None:
            self.misses += 1
        else:
            self.held = raw
            self.misses = 0
        return self.held

    @property
    def missing(self):
        return self.misses > 0

    @property
    def should_retry(self):
        return 0 < self.misses <= START_RETRY_LIMIT


START_RETRY_INTERVAL_MS = 500
START_RETRY_LIMIT = 20  # ~10 s; the slow backstop poll continues after that


class StartBoundary:
    """Remember the last good Start-button edge.

    Explorer removes the taskbar's Start window for a second or two whenever the
    Start menu is open, so "not found" usually means "temporarily hidden", not
    "gone". Widening the bar to the screen edge at that moment covers the real
    button, so a missing reading keeps the previous value and asks for a quick retry.
    """

    def __init__(self, initial=None):
        self.held = initial
        self.misses = 0

    def update(self, raw):
        """Feed a fresh reading (or None); returns the edge to lay out against."""
        if raw is None:
            self.misses += 1
        else:
            self.held = raw
            self.misses = 0
        return self.held

    @property
    def missing(self):
        return self.misses > 0

    @property
    def should_retry(self):
        return 0 < self.misses <= START_RETRY_LIMIT


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
        available = min(available, CAPSULE_MIC_WIDTH if available >= CAPSULE_MIC_WIDTH else CAPSULE_MIN_WIDTH)
    return max(1, available)


def column_layout(width):
    """Return 9 cumulative edges: 0, name, primary, weekly, third, mic, desktop 1-3.

    Columns hidden by the current tier have zero width (equal neighbouring edges).
    """
    width = _clamp_width(width)
    mode = layout_mode(width)
    if mode in COMPACT_MODES:
        with_mic = MIC_BATTERY_COLUMN_WIDTH if mode == CAPSULE_MIC else 0
        name, primary, weekly, mic = _share(
            width, (CAPSULE_NAME_WIDTH, CAPSULE_PRIMARY_WIDTH, CAPSULE_WEEKLY_WIDTH, with_mic)
        )
        columns = [primary, weekly, 0, mic, 0, 0, 0]
    else:
        preferred = dict(LAYOUT_TIERS)[mode]
        name = NAME_COLUMN_MAX
        columns = _share(width - name, preferred)

    edges = [0, name]
    for column_width in columns:
        edges.append(edges[-1] + column_width)
    return tuple(edges)
