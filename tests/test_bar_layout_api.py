import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.bar_layout_api import (
    CAPSULE,
    CAPSULE_MIC,
    CAPSULE_MIC_WIDTH,
    CAPSULE_MIN_WIDTH,
    COMPACT_BELOW,
    LAYOUT_TIERS,
    MAX_PREFERRED_BAR_WIDTH,
    NAME_COLUMN_MAX,
    START_RETRY_LIMIT,
    StartBoundary,
    column_layout,
    layout_mode,
    width_before_start_button,
)


def tier_minimum(name):
    return NAME_COLUMN_MAX + sum(dict(LAYOUT_TIERS)[name])


class WidthBeforeStartButtonTests(unittest.TestCase):
    def test_leaves_a_gap_before_the_start_button(self):
        self.assertEqual(width_before_start_button(-4, start_left=700), 700 + 4 - 8)

    def test_caps_at_the_maximum_width(self):
        self.assertEqual(width_before_start_button(0, start_left=5000), MAX_PREFERRED_BAR_WIDTH)

    def test_never_returns_less_than_one_pixel(self):
        self.assertEqual(width_before_start_button(2000, start_left=100), 1)

    def test_compact_forms_do_not_stretch_over_the_free_space(self):
        free = COMPACT_BELOW - 1  # still too narrow for the leanest tier
        self.assertEqual(width_before_start_button(0, start_left=free + 8), CAPSULE_MIC_WIDTH)
        just_short = CAPSULE_MIC_WIDTH - 1
        self.assertEqual(width_before_start_button(0, start_left=just_short + 8), CAPSULE_MIN_WIDTH)

    def test_capsule_still_shrinks_when_less_space_than_its_cap(self):
        self.assertEqual(width_before_start_button(0, start_left=150 + 8), 150)

    def test_leanest_tier_is_not_capped(self):
        self.assertEqual(width_before_start_button(0, start_left=COMPACT_BELOW + 8), COMPACT_BELOW)


class StartBoundaryTests(unittest.TestCase):
    def test_missing_reading_keeps_the_last_good_edge(self):
        boundary = StartBoundary(881)
        self.assertEqual(boundary.update(None), 881)  # Start menu open: window temporarily gone
        self.assertTrue(boundary.missing)
        self.assertEqual(boundary.update(None), 881)

    def test_fresh_reading_replaces_the_edge_and_clears_the_miss_count(self):
        boundary = StartBoundary(881)
        boundary.update(None)
        self.assertEqual(boundary.update(859), 859)
        self.assertFalse(boundary.missing)
        self.assertEqual(boundary.misses, 0)

    def test_retries_only_while_missing_and_within_the_limit(self):
        boundary = StartBoundary(881)
        self.assertFalse(boundary.should_retry)
        for _ in range(START_RETRY_LIMIT):
            boundary.update(None)
            self.assertTrue(boundary.should_retry)
        boundary.update(None)
        self.assertFalse(boundary.should_retry, "give up quick retries; the slow backstop continues")
        boundary.update(900)
        self.assertFalse(boundary.should_retry)

    def test_unknown_at_startup_stays_unknown_until_first_reading(self):
        boundary = StartBoundary()
        self.assertIsNone(boundary.update(None))
        self.assertTrue(boundary.should_retry)
        self.assertEqual(boundary.update(903), 903)

    def test_held_edge_keeps_the_bar_clear_of_the_start_button(self):
        boundary = StartBoundary(881)
        held = boundary.update(None)
        self.assertEqual(width_before_start_button(-4, start_left=held), 877)


class LayoutModeTests(unittest.TestCase):
    def test_tier_boundaries(self):
        expected = {
            792: "full", 900: "full",
            791: "no_desktops", 576: "no_desktops",
            575: "no_third", 467: "no_third",
            466: CAPSULE_MIC, 277: CAPSULE_MIC,
            276: CAPSULE, 208: CAPSULE, 1: CAPSULE,
        }
        for width, mode in expected.items():
            self.assertEqual(layout_mode(width), mode, width)

    def test_boundaries_are_derived_from_the_column_widths(self):
        self.assertEqual(
            [tier_minimum(name) for name, _ in LAYOUT_TIERS], [792, 576, 467]
        )

    def test_mode_is_monotonic_in_width(self):
        order = [name for name, _ in LAYOUT_TIERS] + [CAPSULE_MIC, CAPSULE]
        ranks = [order.index(layout_mode(width)) for width in range(1, 1000)]
        self.assertEqual(ranks, sorted(ranks, reverse=True))


class ColumnLayoutTests(unittest.TestCase):
    def test_edges_cover_the_whole_width_with_name_and_seven_columns(self):
        for width in (1, 50, 180, 208, 329, 397, 398, 450, 507, 576, 792, 899, 900, 5000):
            edges = column_layout(width)
            expected = min(width, MAX_PREFERRED_BAR_WIDTH)
            self.assertEqual(len(edges), 9, width)
            self.assertEqual(edges[0], 0)
            self.assertEqual(edges[-1], expected, width)
            self.assertEqual(list(edges), sorted(edges), width)

    def test_no_visible_column_is_narrower_than_its_preferred_width_inside_a_tier(self):
        for name, preferred in LAYOUT_TIERS:
            for width in (tier_minimum(name), tier_minimum(name) + 17, MAX_PREFERRED_BAR_WIDTH):
                if layout_mode(width) != name:
                    continue
                edges = column_layout(width)
                self.assertEqual(edges[1], NAME_COLUMN_MAX)
                for index, wanted in enumerate(preferred):
                    got = edges[2 + index] - edges[1 + index]
                    self.assertEqual(wanted == 0, got == 0, (name, width, index))
                    self.assertGreaterEqual(got, wanted, (name, width, index))

    def test_hiding_order_is_desktops_then_third_column_then_mic_last(self):
        def widths(edges):
            # primary, weekly, third, mic, desktop 1-3
            return [edges[i + 1] - edges[i] for i in range(1, 8)]

        full = widths(column_layout(792))
        self.assertTrue(all(full))
        no_desktops = widths(column_layout(700))
        self.assertEqual(no_desktops[4:], [0, 0, 0])
        self.assertTrue(all(no_desktops[:4]), "third column and mic both still shown")
        no_third = widths(column_layout(500))
        self.assertEqual(no_third[2], 0, "third column hidden before the mic battery")
        self.assertGreater(no_third[3], 0, "mic battery survives the loss of the third column")
        self.assertEqual(no_third[4:], [0, 0, 0])
        self.assertTrue(no_third[0] and no_third[1])

    def test_mic_battery_is_the_last_column_to_go(self):
        for width in range(COMPACT_BELOW - 1, CAPSULE_MIC_WIDTH - 1, -1):
            edges = column_layout(width)
            self.assertGreater(edges[5] - edges[4], 0, f"mic battery lost too early at {width}px")
        self.assertEqual(column_layout(CAPSULE_MIC_WIDTH - 1)[5] - column_layout(CAPSULE_MIC_WIDTH - 1)[4], 0)

    def test_capsule_shows_name_and_two_percent_columns_only(self):
        edges = column_layout(CAPSULE_MIN_WIDTH)
        self.assertEqual(edges[:4], (0, 72, 140, 208))
        self.assertEqual(set(edges[3:]), {208})

    def test_capsule_with_mic_keeps_the_battery_column_and_nothing_else(self):
        edges = column_layout(CAPSULE_MIC_WIDTH)
        self.assertEqual(edges[:4], (0, 72, 140, 208))
        self.assertEqual(edges[4], 208, "third column stays hidden")
        self.assertEqual(edges[5] - edges[4], 69, "mic battery column at its preferred width")
        self.assertEqual(set(edges[5:]), {CAPSULE_MIC_WIDTH}, "desktop columns stay hidden")

    def test_capsule_columns_stay_proportional_when_narrower(self):
        edges = column_layout(150)
        name, primary, weekly = edges[1], edges[2] - edges[1], edges[3] - edges[2]
        self.assertEqual(name + primary + weekly, 150)
        self.assertTrue(all(value > 0 for value in (name, primary, weekly)))


if __name__ == "__main__":
    unittest.main()
