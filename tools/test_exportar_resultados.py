"""Confere a publicação das métricas e a exclusão de arquivos pessoais."""

import json
import tempfile
import unittest
from pathlib import Path

from tools.exportar_resultados import clean, export_results, validate_report


class ResultExportTests(unittest.TestCase):
    def test_export_excludes_chats_and_preserves_metrics_and_source_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, kind in (("treino", "train"), ("conversa", "chat")):
                folder = root / "runs" / name
                folder.mkdir(parents=True)
                (folder / "request.json").write_text(json.dumps({"kind": kind, "messages": ["privado"]}), encoding="utf-8")
                (folder / "training_summary.json").write_text(json.dumps({"metrics": {"train_loss": 0.2}}), encoding="utf-8")
            original = (root / "runs/treino/request.json").read_bytes()
            manifest = export_results(root / "runs", root / "publico")
            self.assertEqual([item["id"] for item in manifest["execucoes"]], ["treino"])
            self.assertFalse((root / "publico/execucoes/conversa").exists())
            request = json.loads((root / "publico/execucoes/treino/request.json").read_text(encoding="utf-8"))
            self.assertNotIn("messages", request)
            self.assertEqual((root / "runs/treino/request.json").read_bytes(), original)
            summary = json.loads((root / "publico/execucoes/treino/training_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["metrics"]["train_loss"], 0.2)
            self.assertIn("sha256_fonte", manifest["execucoes"][0]["arquivos"][0])

    def test_clean_removes_context_paths_and_credentials(self):
        source = {"instruction": "pergunta", "items": [{"context_text": "planilha", "match": True}],
                  "best_checkpoint": "C:\\Users\\alguem\\projeto\\checkpoint-1"}
        result = clean(source)
        self.assertNotIn("instruction", result)
        self.assertEqual(result["items"], [{"match": True}])
        self.assertNotIn("alguem", result["best_checkpoint"])
        with self.assertRaises(ValueError):
            clean({"error": "ghp_" + "a" * 35})

    def test_report_validation_rejects_changed_totals_and_duplicate_ids(self):
        report = {"total": 2, "correct": 1, "items": [{"id": "a", "match": True}, {"id": "b", "match": False}]}
        validate_report(report)
        for changed in ({**report, "correct": 2}, {**report, "total": 3},
                        {**report, "items": [report["items"][0], report["items"][0]]}):
            with self.assertRaises(ValueError):
                validate_report(changed)

    def test_paired_results_keep_both_conditions_and_check_each_count(self):
        report = {"total": 1, "without_tools": {"correct": 1}, "with_tools": {"correct": 0},
                  "items": [{"id": "a", "without_tools": {"match": True}, "with_tools": {"match": False}}]}
        validate_report(report)
        with self.assertRaises(ValueError):
            validate_report({**report, "with_tools": {"correct": 1}})


if __name__ == "__main__":
    unittest.main()
