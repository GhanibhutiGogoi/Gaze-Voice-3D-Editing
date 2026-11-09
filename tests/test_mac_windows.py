"""Finding a window through macOS: the choosing is plain logic, and the listing is run for real on a Mac."""

import ctypes
import os
import subprocess
import sys
import threading
import unittest

from gazevoice import mac_windows
from gazevoice.mac_windows import MAX_TITLE_BAR, PAGE_WIDTH_CM, cm_per_point, page_width_cm, pick_window


def window(pid=7, layer=0, x=120.0, y=390.0, width=900.0, height=632.0):
    return {"pid": pid, "layer": layer, "x": x, "y": y, "width": width, "height": height}


class PickWindowTest(unittest.TestCase):
    def test_the_window_with_this_owner_and_size_is_found_title_bar_and_all(self):
        self.assertEqual(pick_window([window()], 7, 900, 600), (120.0, 390.0, 900.0, 632.0))

    def test_a_full_screen_window_has_no_title_bar(self):
        self.assertEqual(pick_window([window(height=600.0)], 7, 900, 600), (120.0, 390.0, 900.0, 600.0))

    def test_only_this_programs_windows_count(self):
        self.assertIsNone(pick_window([window(pid=8)], 7, 900, 600))

    def test_the_menu_bar_strips_a_program_owns_are_skipped(self):
        strips = [window(x=0, y=0, width=1710, height=38), window(layer=25)]
        self.assertEqual(pick_window([*strips, window()], 7, 900, 600), (120.0, 390.0, 900.0, 632.0))
        self.assertIsNone(pick_window(strips, 7, 900, 600))

    def test_a_window_of_another_size_is_not_it(self):
        self.assertIsNone(pick_window([window(width=800.0)], 7, 900, 600))
        self.assertIsNone(pick_window([window(height=599.0)], 7, 900, 600))
        self.assertIsNone(pick_window([window(height=600.0 + MAX_TITLE_BAR + 1)], 7, 900, 600))


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class ListWindowsTest(unittest.TestCase):
    def test_the_list_comes_back_in_plain_numbers(self):
        windows = mac_windows.list_windows()
        self.assertIsInstance(windows, list)
        for found in windows:
            self.assertEqual(set(found), {"pid", "layer", "x", "y", "width", "height"})
            self.assertIsInstance(found["pid"], int)
            self.assertIsInstance(found["width"], float)

    def test_it_can_be_asked_from_a_thread_beside_the_main_one(self):
        found = []
        thread = threading.Thread(target=lambda: found.append(mac_windows.list_windows()))
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(len(found), 1)

    def test_nothing_found_for_a_program_with_no_windows(self):
        self.assertIsNone(mac_windows.window_frame(os.getpid(), 900, 600))


MACBOOK_AIR = ((290.53, 188.93), (1710.0, 1112.0))  # a 13-inch MacBook Air's screen, as macOS says it: mm, then points


class ScreenSizeTest(unittest.TestCase):
    """How wide a point is on a screen: its size in millimetres, as macOS says, over its size in points."""

    def test_a_13_inch_macbook_air_is_about_29_cm_across_1710_points(self):
        self.assertAlmostEqual(cm_per_point(*MACBOOK_AIR) * 1710, 29.05, places=2)

    def test_a_turned_display_agrees_whichever_way_its_sizes_are_given(self):
        # Measured along the diagonal, so millimetres and points given in different orders can't be crossed.
        millimetres, points = MACBOOK_AIR
        self.assertAlmostEqual(cm_per_point(millimetres, points[::-1]), cm_per_point(millimetres, points))
        self.assertAlmostEqual(cm_per_point(millimetres[::-1], points), cm_per_point(millimetres, points))

    def test_the_size_coregraphics_guesses_for_a_display_that_doesnt_say_is_no_size(self):
        # With no size from the display, CoreGraphics works one out at 72 dots an inch: 2.835 a millimetre.
        for points in ((1920.0, 1080.0), (2560.0, 1440.0), (1710.0, 1112.0)):
            guessed = (points[0] / 2.835, points[1] / 2.835)
            with self.subTest(points=points):
                self.assertIsNone(cm_per_point(guessed, points))
                self.assertIsNone(cm_per_point((guessed[0] * 1.01, guessed[1] * 1.01), points))  # near enough to it
        self.assertIsNotNone(cm_per_point((597.0, 336.0), (2560.0, 1440.0)))  # a real 27-inch display

    def test_a_size_that_makes_no_sense_is_none(self):
        # macOS says 0 by 0 for a display it knows no size for, like some projectors and mirrored screens
        for millimetres, points in (((0.0, 0.0), (1710.0, 1112.0)), ((290.5, 188.9), (0.0, 0.0)),
                                    ((-1.0, 188.9), (1710.0, 1112.0)), ((float("nan"), 188.9), (1710.0, 1112.0)),
                                    ((290.5, 188.9), (float("inf"), 1112.0))):
            with self.subTest(millimetres=millimetres, points=points):
                self.assertIsNone(cm_per_point(millimetres, points))


class PageWidthTest(unittest.TestCase):
    """The calibration page's width in centimetres, from the screen under its middle."""

    def setUp(self):
        real = mac_windows._screen_size
        self.addCleanup(setattr, mac_windows, "_screen_size", real)
        self.asked = []
        self.size = MACBOOK_AIR
        mac_windows._screen_size = lambda x, y: self.asked.append((x, y)) or self.size

    def test_it_is_the_pages_points_times_the_width_of_a_point_on_the_screen_under_its_middle(self):
        self.assertAlmostEqual(page_width_cm(0.0, 38.0, 1710.0, 1074.0), 29.05, places=2)
        self.assertEqual(self.asked, [(855.0, 575.0)])

    def test_a_width_no_page_could_have_is_none(self):
        # Narrower than a small laptop's screen or wider than a big monitor's: the screen's size was wrong.
        self.assertEqual(PAGE_WIDTH_CM, (15.0, 150.0))
        per_point = cm_per_point(*MACBOOK_AIR)
        for width_cm, expected in ((10.0, None), (14.9, None), (15.0, 15.0), (150.0, 150.0), (151.0, None), (400.0, None)):
            with self.subTest(width_cm=width_cm):
                found = page_width_cm(0.0, 0.0, width_cm / per_point, 600.0)
                if expected is None:
                    self.assertIsNone(found)
                else:
                    self.assertAlmostEqual(found, expected)

    def test_a_screen_that_doesnt_say_its_size_has_none(self):
        self.size = ((1710.0 / 2.835, 1112.0 / 2.835), (1710.0, 1112.0))
        self.assertIsNone(page_width_cm(0.0, 38.0, 1710.0, 1074.0))
        self.size = None  # no screen under the page
        self.assertIsNone(page_width_cm(0.0, 38.0, 1710.0, 1074.0))


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class ScreenSizeFromMacOsTest(unittest.TestCase):
    def test_the_page_on_the_screen_at_the_top_left_has_a_plain_width_or_none(self):
        found = mac_windows.page_width_cm(0.0, 38.0, 1000.0, 600.0)
        self.assertTrue(found is None or (isinstance(found, float) and 15.0 <= found <= 150.0), found)

    def test_a_page_on_no_screen_has_none(self):
        self.assertIsNone(mac_windows.page_width_cm(-1e7, -1e7, 1000.0, 600.0))

    def test_it_can_be_asked_from_a_thread_beside_the_main_one(self):
        found = []
        thread = threading.Thread(target=lambda: found.append(mac_windows.page_width_cm(0.0, 38.0, 1000.0, 600.0)))
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(len(found), 1)


class WhenMacOsWontSayTest(unittest.TestCase):
    def test_a_failure_to_list_is_no_window_rather_than_a_crash(self):
        real = mac_windows.list_windows
        self.addCleanup(setattr, mac_windows, "list_windows", real)
        def broken():
            raise OSError("no window server")
        mac_windows.list_windows = broken
        self.assertIsNone(mac_windows.window_frame(7, 900, 600))

    def test_any_other_kind_of_failure_is_no_window_too(self):
        # The caller is in the middle of a spoken command. It falls back to the selection only if this comes back.
        real = mac_windows.list_windows
        self.addCleanup(setattr, mac_windows, "list_windows", real)
        for error in (RuntimeError("odd"), TypeError("odd"), KeyError("pid"), ctypes.ArgumentError("odd"), MemoryError()):
            with self.subTest(type(error).__name__):
                def broken():
                    raise error
                mac_windows.list_windows = broken
                self.assertIsNone(mac_windows.window_frame(7, 900, 600))

    def test_a_failure_to_say_how_big_the_screen_is_is_no_size(self):
        real = mac_windows._screen_size
        self.addCleanup(setattr, mac_windows, "_screen_size", real)
        for error in (OSError("no window server"), AttributeError("no such function"), ctypes.ArgumentError("odd")):
            with self.subTest(type(error).__name__):
                def broken(x, y):
                    raise error
                mac_windows._screen_size = broken
                self.assertIsNone(mac_windows.page_width_cm(0.0, 38.0, 1710.0, 1074.0))


if __name__ == "__main__":
    unittest.main()
