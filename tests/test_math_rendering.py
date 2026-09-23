"""
tests/test_math_rendering.py — the maths-rendering wiring.

The model answers in LaTeX (`\\( V = 3 \\)`, `\\[ n = \\frac{m}{M} \\]`). Markdown treats a
backslash before punctuation as an escape, so before this was fixed the delimiters reached
the screen as bare brackets — `(V_{\\text{NaOH}} = …)` — and the underscores in a subscript
were turned into emphasis. The fix lifts every formula out of the text before marked parses
it and typesets it with KaTeX afterwards.

Typesetting happens in the browser, so this file covers what can honestly be covered without
one: the template still loads KaTeX, every place that renders model output goes through the
single `renderMarkdownInto` helper, the model is told how to write formulas, and the
delimiter contract itself (run against the shipped helper block by tests/js/*.js) holds.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.routes import SYSTEM_PROMPT  # noqa: E402

TEMPLATE = ROOT / "templates" / "index.html"
JS_CHECK = ROOT / "tests" / "js" / "math_delimiters_check.js"


class TestTemplateLoadsKatex(unittest.TestCase):
    """KaTeX has to be present, current, and loaded in a working order."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = TEMPLATE.read_text(encoding="utf-8")

    def test_stylesheet_and_scripts_are_referenced(self) -> None:
        for asset in ("dist/katex.min.css", "dist/katex.min.js", "dist/contrib/auto-render.min.js"):
            with self.subTest(asset=asset):
                self.assertIn(asset, self.html)

    def test_assets_are_pinned_and_integrity_checked(self) -> None:
        """The other CDN scripts are pinned by version; these should be too."""
        for asset in ("dist/katex.min.css", "dist/katex.min.js", "dist/contrib/auto-render.min.js"):
            with self.subTest(asset=asset):
                # The tag may be wrapped across lines, so match the whole element.
                tag = re.search(r"<[^>]*%s[^>]*>" % re.escape(asset), self.html, re.S)
                self.assertIsNotNone(tag, f"no tag references {asset}")
                self.assertRegex(tag.group(0), r"katex@\d+\.\d+\.\d+")
                self.assertIn("integrity=", tag.group(0))
                self.assertIn("crossorigin=", tag.group(0))

    def test_core_loads_before_the_auto_render_extension(self) -> None:
        """auto-render reads the `katex` global at load time, so order is not cosmetic."""
        self.assertLess(self.html.index("dist/katex.min.js"),
                        self.html.index("dist/contrib/auto-render.min.js"))

    def test_katex_css_cannot_be_overridden_by_a_broken_formula(self) -> None:
        self.assertIn(".katex-display", self.html)


class TestSingleRenderingPath(unittest.TestCase):
    """One helper renders model output; nothing bypasses it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = TEMPLATE.read_text(encoding="utf-8")

    def test_helper_is_defined(self) -> None:
        self.assertIn("function renderMarkdownInto(", self.html)
        self.assertIn("function protectMath(", self.html)
        self.assertIn("function typesetMath(", self.html)

    def test_the_analysis_panel_uses_it(self) -> None:
        self.assertIn("renderMarkdownInto($('analysis-output'), d.analysis)", self.html)

    def test_a_saved_analysis_uses_it(self) -> None:
        self.assertIn("renderMarkdownInto($('view-modal-body'), item.markdown)", self.html)

    def test_markdown_is_parsed_in_exactly_one_place(self) -> None:
        """A second marked.parse call site would silently lose formulas again."""
        self.assertEqual(self.html.count("marked.parse("), 1)
        self.assertIn("marked.parse(safe)", self.html)

    def test_the_helper_sanitises_and_typesets_in_that_order(self) -> None:
        start = self.html.index("function renderMarkdownInto(")
        body = self.html[start:start + 700]
        self.assertLess(body.index("DOMPurify.sanitize"), body.index("typesetMath(el)"))

    def test_summary_bullets_are_typeset_too(self) -> None:
        self.assertIn("typesetMath($('summary-body'))", self.html)


class TestModelIsToldHowToWriteMaths(unittest.TestCase):
    def test_the_system_prompt_asks_for_delimited_latex(self) -> None:
        self.assertIn("LaTeX", SYSTEM_PROMPT)
        self.assertIn(r"\(", SYSTEM_PROMPT)
        self.assertIn(r"\[", SYSTEM_PROMPT)

    def test_the_prompt_example_survived_python_string_escaping(self) -> None:
        r"""A single backslash in the source would have become a tab: `\text` is a tab escape.

        The prompt is a normal (not raw) string literal, so the LaTeX in it must be doubled —
        this is the test that catches a regression to `\text`.
        """
        self.assertIn(r"\text{acid}", SYSTEM_PROMPT)
        self.assertIn(r"\frac", SYSTEM_PROMPT)
        self.assertNotIn("\t", SYSTEM_PROMPT)


class TestDelimiterContract(unittest.TestCase):
    """Runs the shipped helper block under node, if node is available."""

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_delimiters_are_parked_before_markdown_and_restored_after(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(JS_CHECK)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
