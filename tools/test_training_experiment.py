import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark.run_baseline import score_rows
from tools.training_experiment import (
    _assert_pilot_input_identity,
    _validation_report_matches,
)


class PilotProvenanceTests(unittest.TestCase):
    def test_resume_rejects_changed_validation_hash_or_prompt(self):
        hashes = {"train": "train-a", "validation": "validation-a", "test": "unused"}
        manifest = {
            "input_identity": {
                "data_sha256_train": "train-a",
                "data_sha256_validation": "validation-a",
                "prompt_fingerprint": "prompt-a",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            pilot = Path(tmp)
            with patch("tools.training_experiment.baseline.prompt_fingerprint", return_value="prompt-a"):
                self.assertEqual(_assert_pilot_input_identity(pilot, manifest, hashes), manifest["input_identity"])
            with patch("tools.training_experiment.baseline.prompt_fingerprint", return_value="prompt-a"):
                with self.assertRaisesRegex(ValueError, "changed"):
                    _assert_pilot_input_identity(pilot, manifest, {**hashes, "validation": "validation-b"})
            with patch("tools.training_experiment.baseline.prompt_fingerprint", return_value="prompt-b"):
                with self.assertRaisesRegex(ValueError, "changed"):
                    _assert_pilot_input_identity(pilot, manifest, hashes)

    def test_unproven_existing_artifacts_cannot_bind_to_current_inputs(self):
        hashes = {"train": "train-a", "validation": "validation-a", "test": "unused"}
        with tempfile.TemporaryDirectory() as tmp:
            pilot = Path(tmp)
            (pilot / "validation_base.json").write_text("{}", encoding="utf-8")
            with patch("tools.training_experiment.baseline.prompt_fingerprint", return_value="prompt-a"):
                with self.assertRaisesRegex(ValueError, "without saved"):
                    _assert_pilot_input_identity(pilot, {}, hashes)

    def test_resume_does_not_reuse_answers_for_changed_validation_records(self):
        rows = [{"id": "a", "instruction": "Sum A", "context_text": "A: amount", "expected": "=SUM(A:A)"}]
        report = score_rows(rows, ["=SUM(A:A)"], "held-out")
        report["condition"] = "base"
        self.assertTrue(_validation_report_matches(report, "base", rows))
        changed = [dict(rows[0], context_text="A: other")]
        self.assertFalse(_validation_report_matches(report, "base", changed))
        report["items"][0]["input_fingerprint"] = "wrong"
        self.assertFalse(_validation_report_matches(report, "base", rows))


if __name__ == "__main__":
    unittest.main()
