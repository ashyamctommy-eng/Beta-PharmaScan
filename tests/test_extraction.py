"""
tests/test_extraction.py — deterministic tests for core/extract.py.

    python -m unittest discover -s tests -v

PDF fixtures need fpdf2 (requirements-dev.txt); those tests skip cleanly without
it. DOCX/PPTX/TXT tests use the runtime dependencies.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.extract import (  # noqa: E402
    ExtractionError,
    MAX_DUP_LINE_OCCURRENCES,
    estimate_tokens,
    extract_document,
)
from tests import fixtures  # noqa: E402

try:
    import fpdf  # noqa: F401
    HAVE_FPDF = True
except ImportError:
    HAVE_FPDF = False

try:
    import docx  # noqa: F401
    HAVE_DOCX = True
except ImportError:
    HAVE_DOCX = False

try:
    import pptx  # noqa: F401
    HAVE_PPTX = True
except ImportError:
    HAVE_PPTX = False


class TempFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()


class TestEstimate(unittest.TestCase):
    def test_chars_over_four(self) -> None:
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)


@unittest.skipUnless(HAVE_FPDF, "fpdf2 not installed (requirements-dev.txt)")
class TestPdfExtraction(TempFixture):
    def test_structure_pages_and_furniture(self) -> None:
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=12)
        result = extract_document(path)

        self.assertEqual(result.kind, "pdf")
        # page count must match the file itself (independent source of truth)
        from pypdf import PdfReader
        self.assertEqual(result.pages, len(PdfReader(str(path)).pages))
        self.assertGreaterEqual(result.pages, 12)
        self.assertFalse(result.scanned, "a text PDF must not be flagged scanned")
        self.assertTrue(result.has_structure, "headings should be detected")

        headings = [s.heading for s in result.sections]
        self.assertIn("1. Introduction to Pharmacokinetics", headings)
        self.assertIn("3.1 Phase I Reactions", headings)
        self.assertGreaterEqual(len(result.sections), 7)

        # every section carries a usable page number and an id
        for section in result.sections:
            self.assertGreaterEqual(section.page, 1)
            self.assertTrue(section.id.startswith("s"))
            self.assertGreater(section.tokens, 0)

    def test_running_header_and_footer_are_stripped(self) -> None:
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=12)
        result = extract_document(path)
        self.assertNotIn(fixtures.HEADER, result.text, "running header must be removed")
        self.assertNotIn("- 4 -", result.text, "page numbers must be removed")
        self.assertTrue(
            any("header/footer" in w for w in result.warnings),
            f"expected a furniture warning, got {result.warnings}",
        )

    def test_body_text_repeated_thrice_survives(self) -> None:
        """Regression: repetition alone must not delete body text.

        The spike's naive rule (repeated anywhere ⇒ furniture) deleted real body
        lines. Only repetition *inside the top/bottom band* is furniture.
        """
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=12)
        result = extract_document(path)
        self.assertIn(
            fixtures.REPEATED_THRICE.split(".")[0], result.text,
            "a long body sentence repeated 3x must survive extraction",
        )

    def test_heavy_duplication_is_collapsed_and_reported(self) -> None:
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=12)
        result = extract_document(path)
        self.assertGreater(result.duplicate_lines_collapsed, MAX_DUP_LINE_OCCURRENCES)
        self.assertTrue(any("Collapsed" in w for w in result.warnings))

    def test_skeleton_is_much_smaller_than_the_document(self) -> None:
        """The outline-first design depends on this ratio being small."""
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=12)
        result = extract_document(path)
        skeleton_tokens = estimate_tokens(result.skeleton())
        self.assertLess(
            skeleton_tokens, result.tokens * 0.3,
            f"skeleton {skeleton_tokens} vs document {result.tokens} — outline pass is not cheap enough",
        )
        self.assertIn("(p.", result.skeleton())

    def test_heading_less_pdf_falls_back_to_windows(self) -> None:
        path = fixtures.build_pdf_without_headings(self.dir / "prose.pdf", pages=4)
        result = extract_document(path)
        self.assertFalse(result.has_structure)
        self.assertGreaterEqual(len(result.sections), 1)
        self.assertTrue(result.sections[0].heading.startswith("Part "))
        self.assertTrue(any("No headings found" in w for w in result.warnings))

    def test_scanned_like_pdf_is_reported_not_summarised(self) -> None:
        path = fixtures.build_scanned_like_pdf(self.dir / "scan.pdf")
        result = extract_document(path)
        self.assertTrue(result.scanned)
        self.assertTrue(any("scanned" in w.lower() for w in result.warnings))

    def test_preview_payload_shape(self) -> None:
        path = fixtures.build_pdf(self.dir / "notes.pdf", pages=8)
        preview = extract_document(path).preview(estimated_cost_tokens=1234)
        for key in ("kind", "pages", "tokens", "sections", "skeleton_tokens",
                    "estimated_cost_tokens", "warnings", "scanned", "has_structure"):
            self.assertIn(key, preview)
        self.assertEqual(preview["estimated_cost_tokens"], 1234)
        self.assertEqual(preview["kind"], "pdf")


@unittest.skipUnless(HAVE_DOCX, "python-docx not installed")
class TestDocxExtraction(TempFixture):
    def test_headings_styles_and_table(self) -> None:
        path = fixtures.build_docx(self.dir / "notes.docx")
        result = extract_document(path)
        self.assertEqual(result.kind, "docx")
        headings = [s.heading for s in result.sections]
        self.assertIn("Pharmacology Revision Notes", headings)
        self.assertIn("Bioavailability", headings)
        self.assertIn("Clearance", headings)
        self.assertIn("CYP3A4 | Midazolam | Ketoconazole", result.text)
        self.assertGreater(result.tokens, 20)
        self.assertFalse(result.scanned)


@unittest.skipUnless(HAVE_PPTX, "python-pptx not installed")
class TestPptxExtraction(TempFixture):
    def test_slide_per_section(self) -> None:
        path = fixtures.build_pptx(self.dir / "slides.pptx")
        result = extract_document(path)
        self.assertEqual(result.kind, "pptx")
        self.assertEqual(result.pages, 3)
        headings = [s.heading for s in result.sections]
        self.assertEqual(headings, ["Pharmacokinetics", "Bioavailability", "Half-life"])
        self.assertIn("Pharmacokinetics", result.sections[0].heading)


class TestPlainExtraction(TempFixture):
    def test_markdown_headings(self) -> None:
        path = fixtures.build_plain_text(self.dir / "notes.md")
        result = extract_document(path)
        self.assertEqual(result.kind, "text")
        headings = [s.heading for s in result.sections]
        self.assertIn("Pharmacokinetics", headings)
        self.assertIn("Clearance", headings)


class TestRefusals(TempFixture):
    def test_legacy_doc_is_refused_with_advice(self) -> None:
        path = self.dir / "old.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        with self.assertRaises(ExtractionError) as ctx:
            extract_document(path)
        self.assertIn("save as", str(ctx.exception))

    def test_unsupported_extension_is_refused(self) -> None:
        path = self.dir / "image.png"
        path.write_bytes(b"\x89PNG")
        with self.assertRaises(ExtractionError):
            extract_document(path)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ExtractionError):
            extract_document(self.dir / "nope.pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
