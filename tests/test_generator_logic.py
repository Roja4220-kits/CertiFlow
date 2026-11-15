"""Layout/font logic of certificate_generator and template_ocr, with a recording fake page.

This checks WHERE and HOW BIG text would be drawn and which font path is taken.
It does not render a PDF (that needs PyMuPDF; see tests/test_app.py).
Run:  python -m unittest tests.test_generator_logic -v
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import testenv  # noqa: F401

import certificate_generator as g  # noqa: E402
import template_ocr as ocr  # noqa: E402

DEJAVU = testenv.FONT or ""   # any real .ttf font; the name is historical


class FakePage:
    def __init__(self, width=1000, has_html=True):
        self.rect = SimpleNamespace(width=width, height=700)
        self.calls = []
        if has_html:
            self.insert_htmlbox = self._html

    def insert_text(self, point, text, **kw):
        self.calls.append(("text", point, text, kw))

    def _html(self, rect, markup, **kw):
        self.calls.append(("html", rect, markup, kw))
        return (10, 1)


def cfg(**over):
    base = g.normalize_position({"align": "center", "font_size": 20}, "f")
    base.update(over)
    return base


class DrawTextTests(unittest.TestCase):
    def setUp(self):
        # 10 units per character per 20pt font => width = 0.5 * size * len(text)
        self.patches = [
            patch.object(g.fitz, "get_text_length", lambda text, fontname, fontsize: 0.5 * fontsize * len(text), create=True),
            patch.object(g.fitz, "Point", lambda x, y: (x, y), create=True),
            patch.object(g.fitz, "Rect", lambda *a: a, create=True),
            patch.object(g.fitz, "Archive", lambda p: ("archive", p), create=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def x_of(self, page):
        return page.calls[-1][1][0], page.calls[-1][3]["fontsize"]

    def test_center_left_right_anchoring(self):
        text = "Hello"          # width at 20pt = 50
        for align, expected in (("center", 500 - 25), ("left", 400), ("right", 600 - 50)):
            page = FakePage()
            g.draw_text(page, text, cfg(align=align), cx=500, cy=300, box_width=200)
            self.assertAlmostEqual(self.x_of(page)[0], expected, places=3, msg=align)

    def test_long_text_shrinks_to_fit_80_percent_of_page(self):
        page = FakePage(width=1000)
        g.draw_text(page, "x" * 200, cfg(font_size=30), cx=500, cy=300)   # 3000 wide at 30pt
        x, size = self.x_of(page)
        self.assertAlmostEqual(0.5 * size * 200, 800, places=1)           # exactly the 80% limit
        self.assertAlmostEqual(x, 100, places=1)

    def test_neighbour_limit_prevents_overlap(self):
        page = FakePage()
        g.draw_text(page, "x" * 30, cfg(font_size=20, max_width=120), cx=500, cy=300)   # 300 wide at 20pt
        _, size = self.x_of(page)
        self.assertAlmostEqual(0.5 * size * 30, 120, places=1)
        with self.assertRaises(ValueError):                  # would need < 6pt: refused, not squashed
            g.draw_text(FakePage(), "x" * 60, cfg(font_size=20, max_width=120), cx=500, cy=300)

    def test_text_that_cannot_fit_is_reported(self):
        with self.assertRaises(ValueError):
            g.draw_text(FakePage(width=1000), "x" * 5000, cfg(font_size=20), cx=500, cy=300)

    def test_non_latin_without_font_gives_helpful_error(self):
        with patch.object(g, "UNICODE_FONT_PATH", g.Path("/nonexistent.ttf")):
            with self.assertRaises(ValueError) as ctx:
                g.draw_text(FakePage(), "రోజా", cfg(), cx=500, cy=300)
        self.assertIn("Upload a font", str(ctx.exception))

    def test_missing_custom_font_is_reported(self):
        with self.assertRaises(ValueError) as ctx:
            g.draw_text(FakePage(), "Roja", cfg(font_name="custom:" + "a" * 32), cx=500, cy=300)
        self.assertIn("no longer available", str(ctx.exception))

    @unittest.skipUnless(os.path.exists(DEJAVU), "needs DejaVuSans.ttf")
    def test_custom_font_latin_text_uses_insert_text_with_font_file(self):
        font_id = "custom:" + "b" * 32
        token = g._FONT_FILES.set({font_id: g.Path(DEJAVU)})
        try:
            page = FakePage()
            g.draw_text(page, "Roja", cfg(font_name=font_id), cx=500, cy=300)
        finally:
            g._FONT_FILES.reset(token)
        kind, _pt, _text, kw = page.calls[-1]
        self.assertEqual(kind, "text")
        self.assertEqual(kw["fontfile"], DEJAVU)

    @unittest.skipUnless(os.path.exists(DEJAVU), "needs DejaVuSans.ttf")
    def test_complex_script_goes_through_shaped_html_path(self):
        font_id = "custom:" + "c" * 32
        token = g._FONT_FILES.set({font_id: g.Path(DEJAVU)})
        try:
            page = FakePage()
            g.draw_text(page, "నమస్తే", cfg(font_name=font_id), cx=500, cy=300)   # Telugu
        finally:
            g._FONT_FILES.reset(token)
        kind, _rect, markup, kw = page.calls[-1]
        self.assertEqual(kind, "html")
        self.assertIn("నమస్తే", markup)
        self.assertIn("font-family:cfont", markup)
        self.assertIn(os.path.basename(DEJAVU), kw["css"])
        self.assertEqual(kw["scale_low"], 0)

    @unittest.skipUnless(os.path.exists(DEJAVU), "needs DejaVuSans.ttf")
    def test_complex_script_on_old_pymupdf_explains_upgrade(self):
        font_id = "custom:" + "d" * 32
        token = g._FONT_FILES.set({font_id: g.Path(DEJAVU)})
        try:
            with self.assertRaises(ValueError) as ctx:
                g.draw_text(FakePage(has_html=False), "नमस्ते", cfg(font_name=font_id), cx=500, cy=300)
        finally:
            g._FONT_FILES.reset(token)
        self.assertIn("PyMuPDF 1.24", str(ctx.exception))

    def test_html_is_escaped(self):
        font_id = "custom:" + "e" * 32
        if not os.path.exists(DEJAVU):
            self.skipTest("needs DejaVuSans.ttf")
        token = g._FONT_FILES.set({font_id: g.Path(DEJAVU)})
        try:
            page = FakePage()
            g.draw_text(page, "रोजा <script>", cfg(font_name=font_id), cx=500, cy=300)
        finally:
            g._FONT_FILES.reset(token)
        self.assertNotIn("<script>", page.calls[-1][2])


class FontNameRules(unittest.TestCase):
    def test_valid_and_invalid_names(self):
        for ok in ("helv", "tibi", "custom:" + "0" * 32, "bundled:noto_sans_telugu"):
            self.assertTrue(g.is_valid_font_name(ok), ok)
        for bad in ("Arial", "custom:xyz", "custom:" + "0" * 31, "../etc/passwd", "bundled:../x", ""):
            self.assertFalse(g.is_valid_font_name(bad), bad)

    def test_position_validation(self):
        self.assertEqual(g.normalize_position({"font_name": "custom:" + "a" * 32}, "f")["font_name"], "custom:" + "a" * 32)
        for bad in ({"font_size": 3}, {"font_size": 500}, {"align": "top"}, {"color": "red"}, {"x": 5}, {"font_name": "nope"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                g.normalize_position(bad, "f")


class OcrPositionTests(unittest.TestCase):
    def test_only_fields_on_the_same_line_limit_each_other(self):
        # geometry measured from the real sample template by the OCR run
        fields = [
            {"field": "course_name", "x": 571, "y": 565, "width": 459, "height": 51, "color": "#192050"},
            {"field": "start_date", "x": 764, "y": 677, "width": 267, "height": 35, "color": "#192050"},
            {"field": "end_date", "x": 1124, "y": 677, "width": 251, "height": 35, "color": "#192050"},
        ]
        positions = ocr.image_positions_from_detection({"image_height": 1131, "fields": fields})
        self.assertNotIn("max_width", positions["course_name"])        # nothing else on its line
        self.assertIn("max_width", positions["start_date"])            # shares a line with end_date
        self.assertIn("max_width", positions["end_date"])
        self.assertGreater(positions["course_name"]["font_size"], positions["start_date"]["font_size"])
        self.assertEqual(positions["start_date"]["color"], "#192050")


if __name__ == "__main__":
    unittest.main(verbosity=2)