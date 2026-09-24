"""
tests/test_ai_json_repair.py — LaTeX inside model JSON.

The short-notes pipeline asks the model for JSON, and a model that writes LaTeX inside a
JSON string breaks it in two different ways:

  * `\\(` `\\[` `\\,` are not legal JSON escapes, so `json.loads` raises and the whole
    section is thrown away (a wasted call, a missing topic);
  * `\\frac` is `\\f` + "rac" — a form feed — and `\\times` is a tab plus "imes", which
    parse *successfully* and silently corrupt the note the student reads.

`repair_latex_escapes` doubles only the backslashes that cannot have been intentional, so
the JSON survives and the LaTeX reaches the UI intact (where KaTeX typesets it).

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ai import parse_json_lenient, repair_latex_escapes  # noqa: E402


class TestWhatWasBrokenNowWorks(unittest.TestCase):
    """Each case below is a reproduction of real damage, not a hypothetical."""

    def parse(self, raw: str) -> dict | None:
        return parse_json_lenient(raw)

    def test_a_formula_no_longer_arrives_as_a_form_feed(self) -> None:
        """`\\frac` used to parse as form-feed + "rac" — valid JSON, corrupted note."""
        out = self.parse(r'{"bullets": [{"text": "Formula: \frac{m}{M} gives moles (p.9).", "page": 9}]}')
        self.assertIsNotNone(out)
        self.assertIn(r"\frac{m}{M}", out["bullets"][0]["text"])
        self.assertNotIn("\x0c", out["bullets"][0]["text"])

    def test_times_no_longer_arrives_as_a_tab(self) -> None:
        out = self.parse(r'{"bullets": [{"text": "Rate \times time (p.1).", "page": 1}]}')
        self.assertIn(r"\times", out["bullets"][0]["text"])
        self.assertNotIn("\t", out["bullets"][0]["text"])

    def test_delimiters_no_longer_kill_the_whole_section(self) -> None:
        """`\\(` is an illegal escape, so before this the object did not parse at all."""
        raw = r'{"bullets": [{"text": "Half-life \(t_{1/2}\) governs dosing (p.4).", "page": 4}]}'
        self.assertRaises(json.JSONDecodeError, json.loads, raw)   # the trap, demonstrated
        out = self.parse(raw)
        self.assertIsNotNone(out, "the section was thrown away")
        self.assertIn(r"\(t_{1/2}\)", out["bullets"][0]["text"])

    def test_greek_and_symbols_survive(self) -> None:
        out = self.parse(r'{"t": "\nu, \theta, \nabla, \upsilon, \beta, \rho"}')
        for command in (r"\nu", r"\theta", r"\nabla", r"\upsilon", r"\beta", r"\rho"):
            self.assertIn(command, out["t"], command)

    def test_a_formula_in_a_table_cell_survives(self) -> None:
        out = self.parse(
            r'{"drug_table": [{"name": "Theophylline", "mechanism": "half-life \propto 1/CL",'
            r' "note": "V_d \times weight"}]}'
        )
        self.assertIn(r"\propto", out["drug_table"][0]["mechanism"])


class TestWhatMustNotBeTouched(unittest.TestCase):
    def test_a_real_newline_stays_a_newline(self) -> None:
        """`\\n` before a word is legitimate — repairing it would show a literal backslash."""
        out = parse_json_lenient(r'{"t": "First line.\nSecond line starts here."}')
        self.assertEqual(out["t"], "First line.\nSecond line starts here.")

    def test_doubled_backslashes_from_our_own_json_dumps_are_untouched(self) -> None:
        """Notes are stored as json.dumps output and re-parsed on every read."""
        stored = json.dumps({"t": r"Use \frac{m}{M} and \times."})
        self.assertEqual(repair_latex_escapes(stored), stored)
        self.assertEqual(parse_json_lenient(stored)["t"], r"Use \frac{m}{M} and \times.")

    def test_a_unicode_escape_that_is_real_is_untouched(self) -> None:
        out = parse_json_lenient(r'{"t": "an em dash \u2014 here"}')
        self.assertEqual(out["t"], "an em dash — here")

    def test_an_unknown_command_that_looks_like_a_newline_is_left_alone(self) -> None:
        """Only the listed command names are repaired — anything else keeps JSON's meaning."""
        out = parse_json_lenient(r'{"t": "line one\nable was I"}')
        self.assertEqual(out["t"], "line one\nable was I")

    def test_plain_json_is_untouched(self) -> None:
        raw = '{"usable": true, "bullets": [{"text": "No maths here (p.2).", "page": 2}]}'
        self.assertEqual(repair_latex_escapes(raw), raw)


class TestTheEarlierRecoveryStillWorks(unittest.TestCase):
    def test_fenced_json(self) -> None:
        self.assertEqual(parse_json_lenient('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_a_sentence_around_it(self) -> None:
        self.assertEqual(parse_json_lenient('Here you go: {"a": 2} hope that helps'), {"a": 2})

    def test_no_json(self) -> None:
        self.assertIsNone(parse_json_lenient("no json here"))
        self.assertIsNone(parse_json_lenient(""))

    def test_a_non_dict_json_document_is_not_a_payload(self) -> None:
        self.assertIsNone(parse_json_lenient("[1, 2, 3]"))


class TestThePromptTellsTheModelTheSameThing(unittest.TestCase):
    """The repair is the safety net; the instruction is the fix."""

    def test_the_notes_prompts_ask_for_unicode_maths_instead_of_latex(self) -> None:
        from core.summarise import REDUCE_SYSTEM, SECTION_SYSTEM
        for name, prompt in (("SECTION_SYSTEM", SECTION_SYSTEM), ("REDUCE_SYSTEM", REDUCE_SYSTEM)):
            with self.subTest(prompt=name):
                self.assertIn("LaTeX", prompt)
                self.assertIn("Unicode", prompt)
                self.assertIn("t½", prompt)

    def test_the_json_reminder_says_how_to_escape_a_backslash(self) -> None:
        from core.ai import JSON_REMINDER
        self.assertIn(r"\\", JSON_REMINDER)


class TestNoPromptIsSecretlyCorrupted(unittest.TestCase):
    r"""A single backslash in a non-raw Python string becomes a control character.

    `\text` is a tab, `\frac` is a form feed, `\nu` is a newline — so a prompt can be
    silently mangled by its own Python literal, which happened twice while writing these.
    """

    def prompts(self) -> dict[str, str]:
        from api.routes import SYSTEM_PROMPT
        from core.summarise import OUTLINE_SYSTEM, REDUCE_SYSTEM, SECTION_SYSTEM
        return {
            "SYSTEM_PROMPT": SYSTEM_PROMPT,
            "SECTION_SYSTEM": SECTION_SYSTEM,
            "REDUCE_SYSTEM": REDUCE_SYSTEM,
            "OUTLINE_SYSTEM": OUTLINE_SYSTEM,
        }

    def test_no_control_characters(self) -> None:
        import re
        for name, prompt in self.prompts().items():
            with self.subTest(prompt=name):
                found = re.search(r"[\x00-\x08\x0b-\x1f]", prompt)
                self.assertIsNone(found, f"{name} contains control character {found.group(0)!r}"
                                          if found else None)

    def test_no_tab_that_was_meant_to_be_a_command(self) -> None:
        import re
        for name, prompt in self.prompts().items():
            with self.subTest(prompt=name):
                found = re.search(r"\t[A-Za-z]", prompt)
                self.assertIsNone(found, f"{name} has a tab before a letter — a mangled \\t* command"
                                         if found else None)


if __name__ == "__main__":
    unittest.main()
