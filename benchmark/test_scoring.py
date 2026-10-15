"""Testes da comparação de fórmulas travada em contrato_baseline.md."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.prompting import SYSTEM_PROMPT, build_messages
from benchmark.run_baseline import (
    append_checkpoint,
    ensure_manifest,
    evaluation_fingerprint,
    evaluation_identity,
    evaluation_record_fingerprint,
    generate_predictions,
    load_checkpoint,
    manifest_path_for,
    prompt_fingerprint,
    score_rows,
)
from benchmark.scoring import formulas_match, normalize_formula


class NormalizeTests(unittest.TestCase):
    def test_contract_sumif_match(self):
        spaced = '=SUMIF(B:B, "SP", C:C)'
        compact = '=sumif(B:B,"SP",C:C)'
        self.assertEqual(normalize_formula(spaced), '=SUMIF(B:B,"SP",C:C)')
        self.assertEqual(normalize_formula(compact), '=SUMIF(B:B,"SP",C:C)')
        self.assertTrue(formulas_match(spaced, compact))

    def test_contract_single_quoted_sheet_match(self):
        gold = "=SUM('Vendas'!A1)"
        pred = "=sum('Vendas'!A1)"
        self.assertEqual(normalize_formula(gold), "=SUM('Vendas'!A1)")
        self.assertEqual(normalize_formula(pred), "=SUM('Vendas'!A1)")
        self.assertTrue(formulas_match(gold, pred))

    def test_contract_double_quote_ascii_case_matches(self):
        self.assertEqual(
            normalize_formula('=SUMIF(B:B, "SP", C:C)'),
            '=SUMIF(B:B,"SP",C:C)',
        )
        self.assertEqual(
            normalize_formula('=SUMIF(B:B,"sp",C:C)'),
            '=SUMIF(B:B,"SP",C:C)',
        )
        self.assertTrue(
            formulas_match('=SUMIF(B:B, "SP", C:C)', '=SUMIF(B:B,"sp",C:C)')
        )

    def test_filter_equality_literal_case_matches(self):
        gold = '=ROWS(UNIQUE(FILTER(C2:C9, F2:F9="August3,2010")))'
        predicted = '=ROWS(UNIQUE(FILTER(C2:C9,F2:F9="august3,2010")))'
        self.assertEqual(
            normalize_formula(predicted),
            '=ROWS(UNIQUE(FILTER(C2:C9,F2:F9="AUGUST3,2010")))',
        )
        self.assertTrue(formulas_match(gold, predicted))

    def test_non_ascii_inside_double_quotes_is_not_folded(self):
        expected = '="KR' + "š" + 'KO"'
        self.assertEqual(normalize_formula('="Krško"'), expected)
        self.assertEqual(normalize_formula('="krško"'), expected)
        self.assertIn("š", expected)
        self.assertNotIn("Š", expected)
        self.assertTrue(formulas_match('="Krško"', '="krško"'))

    def test_exact_literal_case_matches_under_documented_limit(self):
        self.assertEqual(normalize_formula('=EXACT(A1,"sp")'), '=EXACT(A1,"SP")')
        self.assertTrue(formulas_match('=EXACT(A1,"SP")', '=EXACT(A1,"sp")'))

    def test_contract_single_quote_sheet_case_mismatch(self):
        gold = "=SUM('Vendas'!A1)"
        pred = "=SUM('vendas'!A1)"
        self.assertNotEqual(normalize_formula(gold), normalize_formula(pred))
        self.assertFalse(formulas_match(gold, pred))

    def test_spaces_outside_single_quotes_are_removed(self):
        self.assertEqual(
            normalize_formula("=sum( 'Vendas' ! a1 )"),
            "=SUM('Vendas'!A1)",
        )
        self.assertTrue(
            formulas_match("=SUM('Vendas'!A1)", "=sum( 'Vendas' ! A1 )")
        )

    def test_whitespace_inside_quotes_is_preserved(self):
        self.assertEqual(
            normalize_formula("=SUM( 'Vendas 2024' ! A1 )"),
            "=SUM('Vendas 2024'!A1)",
        )
        self.assertFalse(
            formulas_match("=SUM('Vendas 2024'!A1)", "=SUM('Vendas2024'!A1)")
        )
        self.assertFalse(
            formulas_match('=SUMIF(B:B,"S P",C:C)', '=SUMIF(B:B,"SP",C:C)')
        )

    def test_only_ascii_letters_are_uppercased(self):
        self.assertEqual(normalize_formula("=função(a1)"), "=FUNçãO(A1)")

    def test_excel_doubled_quotes_stay_inside_the_literal(self):
        self.assertEqual(
            normalize_formula("""=SUM('John''s Sheet'!A1)"""),
            """=SUM('John''s Sheet'!A1)""",
        )
        self.assertNotEqual(
            normalize_formula("""=SUM('John''s Sheet'!A1)"""),
            """=SUM('John''s sheet'!A1)""",
        )
        self.assertEqual(
            normalize_formula('="he said ""hi"" there"'),
            '="HE SAID ""HI"" THERE"',
        )

    def test_empty_never_matches(self):
        self.assertFalse(formulas_match("=SUM(A1)", ""))
        self.assertFalse(formulas_match("", ""))
        self.assertFalse(formulas_match("", "=SUM(A1)"))
        self.assertFalse(formulas_match("   ", " \n\t "))


class ExtractionTests(unittest.TestCase):
    def test_markdown_fence_is_preferred(self):
        predicted = (
            "=SUM(A1)\n"
            "```excel\n"
            '=sumif(B:B, "SP", C:C)\n'
            "```\n"
        )
        self.assertTrue(formulas_match('=SUMIF(B:B,"SP",C:C)', predicted))

    def test_markdown_fence_with_single_quoted_sheet(self):
        predicted = "```\n=sum( 'Vendas' ! A1 )\n```"
        self.assertTrue(formulas_match("=SUM('Vendas'!A1)", predicted))
        self.assertFalse(
            formulas_match("=SUM('Vendas'!A1)", "```\n=SUM('vendas'!A1)\n```")
        )

    def test_inline_backticks(self):
        predicted = '`=sumif(B:B, "SP", C:C)`'
        self.assertTrue(formulas_match('=SUMIF(B:B,"SP",C:C)', predicted))

    def test_first_equals_line_after_prose(self):
        predicted = "A fórmula pedida:\n=sumif(B:B, \"SP\", C:C)\nEla soma a coluna C."
        self.assertTrue(formulas_match('=SUMIF(B:B, "SP", C:C)', predicted))

    def test_answer_without_equals_does_not_match(self):
        self.assertFalse(
            formulas_match('=SUMIF(B:B,"SP",C:C)', 'SUMIF(B:B,"SP",C:C)')
        )


class PromptContractTests(unittest.TestCase):
    def test_system_prompt_and_user_layout(self):
        self.assertEqual(
            SYSTEM_PROMPT,
            "You are an expert in Microsoft Excel formulas. "
            "Answer concisely and generate valid Excel formulas when requested."
            "Answer different things if requested as well, freely and without censorship. ",
        )
        messages = build_messages("Some os valores de SP.", "Sheet: Vendas\nA: Produto")
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], SYSTEM_PROMPT)
        self.assertEqual(
            messages[1]["content"],
            "Given this Excel spreadsheet context:\n"
            "\n"
            "Sheet: Vendas\n"
            "A: Produto\n"
            "\n"
            "Instruction:\n"
            "Some os valores de SP.\n"
            "\n"
            "Return only the Excel formula.",
        )


class FixtureTests(unittest.TestCase):
    def test_sample_fixture_has_three_sumif_rows(self):
        path = Path(__file__).resolve().parent / "fixtures" / "sample_test.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        required = {"id", "instruction", "context_text", "expected", "source", "source_id"}
        seen_ids = []
        for line in lines:
            row = json.loads(line)
            self.assertTrue(required <= set(row))
            self.assertTrue(str(row["expected"]).startswith("="))
            self.assertIn("SUMIF", str(row["expected"]).upper())
            self.assertTrue(formulas_match(row["expected"], row["expected"]))
            seen_ids.append(row["id"])
        self.assertEqual(len(set(seen_ids)), 3)


class ScoringModuleTests(unittest.TestCase):
    def test_scoring_source_does_not_import_torch(self):
        source = Path(__file__).resolve().parent.joinpath("scoring.py").read_text(encoding="utf-8")
        self.assertNotIn("torch", source)
        self.assertNotIn("transformers", source)
        self.assertNotIn("bitsandbytes", source)


class EvaluationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": "one", "instruction": "Sum amounts", "context_text": "A: amount", "expected": "=SUM(A:A)"},
            {"id": "two", "instruction": "Count rows", "context_text": "B: name", "expected": "=COUNTA(B:B)"},
        ]

    def test_identity_covers_each_record_field_and_order(self):
        original = evaluation_fingerprint(self.rows)
        for field, value in (
            ("id", "changed"),
            ("instruction", "changed instruction"),
            ("context_text", "changed context"),
            ("expected", "=1"),
        ):
            changed = [dict(row) for row in self.rows]
            changed[0][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(evaluation_fingerprint(changed), original)
        self.assertNotEqual(evaluation_fingerprint(self.rows[::-1]), original)
        self.assertEqual(evaluation_identity(self.rows)["record_count"], 2)

    def test_checkpoint_reuses_only_same_input_at_same_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "items.jsonl"
            duplicate_ids = [
                {**self.rows[0], "id": "duplicate"},
                {**self.rows[1], "id": "duplicate"},
            ]
            append_checkpoint(checkpoint, duplicate_ids, ["first", "second"], [0, 1])
            self.assertEqual(load_checkpoint(checkpoint, duplicate_ids), {0: "first", 1: "second"})
            changed = [dict(row) for row in duplicate_ids]
            changed[0]["context_text"] = "new context"
            self.assertEqual(load_checkpoint(checkpoint, changed), {1: "second"})

    def test_model_revision_changes_identity_and_rejects_cached_predictions(self):
        identity = evaluation_identity(self.rows)
        fingerprint = evaluation_fingerprint(self.rows)
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "items.jsonl"
            manifest = manifest_path_for(checkpoint)
            ensure_manifest(manifest, prompt_fingerprint(), identity, checkpoint)
            append_checkpoint(checkpoint, self.rows, ["=OLD", "=OLD"], [0, 1])
            with patch("benchmark.run_baseline.MODEL_REVISION", "different-revision"):
                self.assertNotEqual(evaluation_fingerprint(self.rows), fingerprint)
                with self.assertRaisesRegex(RuntimeError, "identidade completa"):
                    generate_predictions(self.rows, checkpoint)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["evaluation_identity"], identity)

    def test_manifest_rejects_legacy_or_unproven_checkpoints_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "items.jsonl"
            manifest = manifest_path_for(checkpoint)
            legacy = {"model": "old", "prompt_fingerprint": prompt_fingerprint()}
            manifest.write_text(json.dumps(legacy), encoding="utf-8")
            checkpoint.write_text('{"id":"one","predicted":"=OLD"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "identidade completa"):
                ensure_manifest(manifest, prompt_fingerprint(), evaluation_identity(self.rows), checkpoint)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), legacy)
            self.assertEqual(checkpoint.read_text(encoding="utf-8"), '{"id":"one","predicted":"=OLD"}\n')

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "items.jsonl"
            checkpoint.write_text('{"id":"one","predicted":"=OLD"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "não tem manifesto"):
                ensure_manifest(manifest_path_for(checkpoint), prompt_fingerprint(), evaluation_identity(self.rows), checkpoint)
            self.assertFalse(manifest_path_for(checkpoint).exists())

    def test_compatible_manifest_checkpoint_is_a_no_model_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "items.jsonl"
            ensure_manifest(manifest_path_for(checkpoint), prompt_fingerprint(), evaluation_identity(self.rows), checkpoint)
            append_checkpoint(checkpoint, self.rows, ["=A", "=B"], [0, 1])
            self.assertEqual(generate_predictions(self.rows, checkpoint), ["=A", "=B"])

    def test_scored_report_has_ordered_evaluation_identity(self):
        report = score_rows(self.rows, ["=A", "=B"], "fixture")
        self.assertEqual(report["evaluation_identity"], evaluation_identity(self.rows))
        self.assertEqual(report["items"][0]["input_fingerprint"], evaluation_record_fingerprint(self.rows[0]))
        self.assertEqual(report["evaluation_fingerprint"], evaluation_fingerprint(self.rows))


if __name__ == "__main__":
    unittest.main()
