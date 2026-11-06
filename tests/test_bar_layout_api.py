import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.bar_layout_api import MAX_PREFERRED_BAR_WIDTH, column_layout, width_before_start_button


class WidthBeforeStartButtonTests(unittest.TestCase):
    def test_leaves_a_gap_before_the_start_button(self):
        self.assertEqual(width_before_start_button(-4, start_left=700), 700 + 4 - 8)

    def test_caps_at_the_maximum_width(self):
        self.assertEqual(width_before_start_button(0, start_left=5000), MAX_PREFERRED_BAR_WIDTH)

    def test_never_returns_less_than_one_pixel(self):
        self.assertEqual(width_before_start_button(2000, start_left=100), 1)


class ColumnLayoutTests(unittest.TestCase):
    def test_edges_cover_the_whole_width_with_name_and_seven_columns(self):
        for width in (1, 50, 180, 400, 792, 899, 900, 5000):
            edges = column_layout(width)
            expected = min(width, MAX_PREFERRED_BAR_WIDTH)
            self.assertEqual(len(edges), 9, width)
            self.assertEqual(edges[0], 0)
            self.assertEqual(edges[-1], expected, width)
            self.assertEqual(list(edges), sorted(edges), width)

    def test_name_column_shrinks_before_the_data_columns_when_narrow(self):
        wide = column_layout(792)
        narrow = column_layout(600)
        self.assertLess(narrow[1], wide[1])
        self.assertGreaterEqual(narrow[1], 50)


if __name__ == "__main__":
    unittest.main()
