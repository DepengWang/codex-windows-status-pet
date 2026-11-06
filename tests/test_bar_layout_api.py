import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.bar_layout_api import (
    CAPSULE,
    CAPSULE_MAX_WIDTH,
    COMPACT_BELOW,
    LAYOUT_TIERS,
    MAX_PREFERRED_BAR_WIDTH,
    NAME_COLUMN_MAX,
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

    def test_capsule_does_not_stretch_over_the_free_space(self):
        free = COMPACT_BELOW - 1  # still too narrow for the leanest tier
        self.assertEqual(width_before_start_button(0, start_left=free + 8), CAPSULE_MAX_WIDTH)

    def test_capsule_still_shrinks_when_less_space_than_its_cap(self):
        self.assertEqual(width_before_start_button(0, start_left=150 + 8), 150)

    def test_leanest_tier_is_not_capped(self):
        self.assertEqual(width_before_start_button(0, start_left=COMPACT_BELOW + 8), COMPACT_BELOW)


class LayoutModeTests(unittest.TestCase):
    def test_tier_boundaries(self):
        expected = {
            792: "full", 900: "full",
            791: "no_desktops", 576: "no_desktops",
            575: "no_mic", 507: "no_mic",
            506: "no_third", 398: "no_third",
            397: CAPSULE, 208: CAPSULE, 1: CAPSULE,
        }
        for width, mode in expected.items():
            self.assertEqual(layout_mode(width), mode, width)

    def test_boundaries_are_derived_from_the_column_widths(self):
        self.assertEqual(
            [tier_minimum(name) for name, _ in LAYOUT_TIERS], [792, 576, 507, 398]
        )

    def test_mode_is_monotonic_in_width(self):
        order = [name for name, _ in LAYOUT_TIERS] + [CAPSULE]
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

    def test_each_tier_hides_exactly_the_agreed_columns(self):
        def widths(edges):
            return [edges[i + 1] - edges[i] for i in range(1, 8)]

        full = widths(column_layout(792))
        self.assertTrue(all(full))
        no_desktops = widths(column_layout(700))
        self.assertEqual(no_desktops[4:], [0, 0, 0])
        self.assertTrue(all(no_desktops[:4]))
        no_mic = widths(column_layout(540))
        self.assertEqual(no_mic[3:], [0, 0, 0, 0])
        self.assertTrue(all(no_mic[:3]))
        no_third = widths(column_layout(450))
        self.assertEqual(no_third[2:], [0, 0, 0, 0, 0])
        self.assertTrue(all(no_third[:2]))

    def test_capsule_shows_name_and_two_percent_columns_only(self):
        edges = column_layout(CAPSULE_MAX_WIDTH)
        self.assertEqual(edges[:4], (0, 72, 140, 208))
        self.assertEqual(set(edges[3:]), {208})

    def test_capsule_columns_stay_proportional_when_narrower(self):
        edges = column_layout(150)
        name, primary, weekly = edges[1], edges[2] - edges[1], edges[3] - edges[2]
        self.assertEqual(name + primary + weekly, 150)
        self.assertTrue(all(value > 0 for value in (name, primary, weekly)))


if __name__ == "__main__":
    unittest.main()
