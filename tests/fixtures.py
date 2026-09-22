"""
tests/fixtures.py — generates the sample documents the extraction tests run on.
Fixtures are built at test time (no binaries in the repo). PDFs need fpdf2,
which lives in requirements-dev.txt.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

# A long body sentence repeated 3x → must SURVIVE (under the collapse threshold).
REPEATED_THRICE = (
    "Bioavailability is reduced by first-pass metabolism in the intestinal wall and liver."
)
# A long body sentence repeated 6x → collapsed, and that must be REPORTED.
REPEATED_OFTEN = (
    "The clinical significance of this topic is examined frequently in the final assessment."
)
# Filler that makes the fixture realistically DENSE (~2000+ chars/page) without
# tripping the duplicate-collapse rule: each line is unique because of its prefix.
DENSE_FILLER = (
    "The mechanism should be linked to the therapeutic indication and to the adverse "
    "effects seen in practice, and candidates should be able to compare agents within "
    "the same class on the basis of their pharmacokinetic parameters."
)
HEADER = "CDACC D.Pharm - Pharmacology - Page notes"


def build_pdf(path: Path, pages: int = 12) -> Path:
    """A realistic multi-page document: running header/footer via FPDF's own
    header()/footer() callbacks (so they appear on *every* page, including pages
    created by automatic page breaks), two heading levels, and controlled
    repetition of two body sentences."""
    from fpdf import FPDF

    sections = [
        ("1. Introduction to Pharmacokinetics",
         "Pharmacokinetics describes what the body does to a drug. The four core processes are "
         "absorption, distribution, metabolism and excretion, collectively ADME."),
        ("1.1 Absorption",
         "Absorption is the movement of drug from the site of administration into systemic "
         "circulation. Oral absorption depends on dissolution and gastric emptying."),
        ("1.2 Bioavailability",
         "Bioavailability (F) is the fraction of an administered dose that reaches systemic "
         "circulation unchanged. Intravenous administration is 100 percent by definition."),
        ("2. Drug Distribution",
         "Distribution is the reversible transfer of drug between blood and tissues. Volume of "
         "distribution relates the amount of drug in the body to its plasma concentration."),
        ("3. Drug Metabolism",
         "Metabolism converts lipophilic drugs into more water-soluble metabolites. Phase I "
         "reactions introduce functional groups; phase II reactions conjugate them."),
        ("3.1 Phase I Reactions",
         "Cytochrome P450 enzymes catalyse oxidation, reduction and hydrolysis. CYP3A4 "
         "metabolises the majority of clinically used drugs and is abundant in the gut wall."),
        ("4. Drug Excretion",
         "Renal excretion involves glomerular filtration, active tubular secretion and passive "
         "tubular reabsorption. Clearance is the volume of plasma cleared per unit time."),
    ]
    counter = {"thrice": 0, "often": 0}

    class NotesPdf(FPDF):
        def header(self) -> None:
            self.set_font("Helvetica", "I", 8)
            self.set_y(8)
            self.set_x(self.l_margin)
            self.multi_cell(180, 5, HEADER)

        def footer(self) -> None:
            self.set_y(-14)
            self.set_font("Helvetica", "I", 8)
            self.set_x(self.l_margin)
            self.multi_cell(180, 5, f"- {self.page_no()} -")

    pdf = NotesPdf()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)

    def write(text: str, height: float, style: str = "", size: float = 10) -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(180, height, text)

    pdf.add_page()
    pdf.ln(60)
    write("CDACC D.Pharm\nPharmacology Unit Notes\nY2S1", 10, "B", 20)

    while pdf.page_no() < pages:
        for heading, paragraph in sections:
            if pdf.page_no() >= pages:
                break
            pdf.add_page()
            pdf.set_y(26)
            write(heading, 8, "B" if heading.count(".") == 1 else "",
                  14 if heading.count(".") == 1 else 11.5)
            write(paragraph, 5.5)
            topic = heading.split(". ", 1)[-1]
            for point in range(5):                     # dense, unique body lines
                write(f"{topic} - point {point + 1}: {DENSE_FILLER}", 5.5)
            if counter["thrice"] < 3:
                write(REPEATED_THRICE, 5.5)
                counter["thrice"] += 1
            if counter["often"] < 6:
                write(REPEATED_OFTEN, 5.5)
                counter["often"] += 1

    pdf.output(str(path))
    return path


def build_pdf_without_headings(path: Path, pages: int = 4) -> Path:
    """Paragraphs only — exercises the token-window fallback."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_font("Helvetica", "", 11)
    for index in range(pages):
        pdf.add_page()
        for _ in range(6):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(180, 5.5, textwrap.fill(
                "This document contains running prose with no headings at all, so the "
                "extractor has to fall back to splitting it by length into parts. " * 2, 90))
    pdf.output(str(path))
    return path


def build_scanned_like_pdf(path: Path) -> Path:
    """A page with no text layer at all — a photo/scan stand-in."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.rect(20, 20, 160, 240)      # a drawing, not text
    pdf.output(str(path))
    return path


def build_pdf_with_numbers(path: Path) -> Path:
    """Body lines that are bare numbers (doses/years) plus decorated page numbers."""
    from fpdf import FPDF

    class NumberedPdf(FPDF):
        def header(self) -> None:
            self.set_font("Helvetica", "I", 8)
            self.set_y(8)
            self.set_x(self.l_margin)
            self.multi_cell(180, 5, HEADER)

    pdf = NumberedPdf()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    for index in range(5):
        pdf.add_page()
        pdf.set_y(26)
        for line in ("Paracetamol 500", "2024",
                     "Amoxicillin dose 250 mg three times daily for five days.",
                     f"Chapter {index + 1} continues with more clinical detail about dosing."):
            pdf.set_font("Helvetica", "", 10)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(180, 5.5, line)
        pdf.set_y(-14)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(180, 5, f"Page {index + 7}")
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(180, 5, f"[{index + 8}]")
    pdf.output(str(path))
    return path


def build_docx(path: Path) -> Path:
    import docx

    document = docx.Document()
    document.add_heading("Pharmacology Revision Notes", level=1)
    document.add_paragraph("Absorption is the movement of drug into systemic circulation.")
    document.add_heading("Bioavailability", level=2)
    document.add_paragraph("F is the fraction of the dose reaching systemic circulation.")
    document.add_heading("Clearance", level=2)
    document.add_paragraph("Clearance is the volume of plasma cleared of drug per unit time.")
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, [
        ("Enzyme", "Substrate", "Inhibitor"),
        ("CYP3A4", "Midazolam", "Ketoconazole"),
        ("CYP2D6", "Codeine", "Fluoxetine"),
    ]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.save(str(path))
    return path


def build_pptx(path: Path) -> Path:
    from pptx import Presentation

    deck = Presentation()
    for title, body in [
        ("Pharmacokinetics", "Absorption, distribution, metabolism, excretion."),
        ("Bioavailability", "F is the fraction of the dose reaching systemic circulation."),
        ("Half-life", "Time for plasma concentration to fall by half."),
    ]:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    deck.save(str(path))
    return path


def build_plain_text(path: Path) -> Path:
    path.write_text(
        "# Pharmacokinetics\n\n"
        "Absorption is the movement of drug into the blood.\n\n"
        "## Bioavailability\n\nF is the fraction reaching systemic circulation.\n\n"
        "## Clearance\n\nVolume of plasma cleared per unit time.\n",
        encoding="utf-8",
    )
    return path
