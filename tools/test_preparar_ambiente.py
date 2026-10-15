"""Testes de instalação sem rede, GPU ou arquivos pessoais."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.preparar_ambiente import MODEL_FILES, download_verified, prepare_data, prepare_model
from tools.verificar_ambiente import check_model


class DownloadTests(unittest.TestCase):
    def test_complete_model_cache_is_reused_without_online_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config.json", "tokenizer_config.json", "tokenizer.json", "model.safetensors"):
                (root / name).write_text("{}", encoding="utf-8")
            with patch("huggingface_hub.snapshot_download", return_value=temp) as download:
                self.assertEqual(prepare_model(), root)
                download.assert_called_once()
                self.assertTrue(download.call_args.kwargs["local_files_only"])

    def test_missing_model_cache_downloads_only_the_requested_revision(self):
        with patch("huggingface_hub.snapshot_download", side_effect=[FileNotFoundError("cache ausente"), "cache novo"]) as download:
            self.assertEqual(prepare_model(), "cache novo")
            self.assertEqual(download.call_count, 2)
            self.assertTrue(download.call_args_list[0].kwargs["local_files_only"])
            self.assertEqual(download.call_args_list[0].kwargs["revision"], download.call_args_list[1].kwargs["revision"])

    def test_model_download_includes_shard_index_and_verifies_all_shards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config.json", "tokenizer_config.json", "tokenizer.json", "parte-1.safetensors"):
                (root / name).write_text("{}", encoding="utf-8")
            (root / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
                "a": "parte-1.safetensors", "b": "parte-2.safetensors",
            }}), encoding="utf-8")
            with patch("huggingface_hub.snapshot_download", return_value=temp) as download:
                prepare_model()
                self.assertIn("*.safetensors.index.json", download.call_args.kwargs["allow_patterns"])
                with self.assertRaisesRegex(ValueError, "parte-2.safetensors"):
                    check_model()
                self.assertEqual(download.call_args.kwargs["allow_patterns"], MODEL_FILES)
                self.assertTrue(download.call_args.kwargs["local_files_only"])
                (root / "parte-2.safetensors").write_text("{}", encoding="utf-8")
                self.assertEqual(check_model(), root)

    def test_verified_download_is_atomic_and_a_second_run_is_offline(self):
        content = b"conteudo verificado"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dados.json"
            with patch("urllib.request.urlopen", return_value=io.BytesIO(content)) as network:
                self.assertTrue(download_verified("https://example.test/data", path, len(content), digest))
                self.assertFalse(download_verified("https://example.test/data", path, len(content), digest))
                self.assertEqual(network.call_count, 1)
            self.assertEqual(path.read_bytes(), content)

    def test_corrupt_or_oversize_download_preserves_the_previous_file(self):
        for content in (b"xyz", b"too long"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "dados.json"
                path.write_bytes(b"versao anterior")
                with patch("urllib.request.urlopen", return_value=io.BytesIO(content)):
                    with self.assertRaises(ValueError):
                        download_verified("https://example.test/data", path, 3, hashlib.sha256(b"abc").hexdigest())
                self.assertEqual(path.read_bytes(), b"versao anterior")
                self.assertEqual([item.name for item in path.parent.iterdir()], ["dados.json"])

    def test_prepare_data_keeps_the_source_split_and_frozen_test_ids(self):
        table = [{"TableName": "Dados", "Table": [["0", "A"], ["1", "Valor"], ["2", "3"]],
                  "t5Formulas": [{"Question": "Somar", "Formula2": "SUM(A2:A2)", "Level": "easy"}]}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def fixture_download(url, path, size, digest):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(table), encoding="utf-8")
            with patch("tools.preparar_ambiente.download_verified", side_effect=fixture_download):
                self.assertEqual(prepare_data(root), {"train": 1, "validation": 1, "test": 1})
            ids = (root / "dataset/FROZEN_TEST_IDS.txt").read_text(encoding="utf-8")
            self.assertEqual(ids, "nl2f-test-000000\n")
            record = json.loads((root / "dataset/canonical/train.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["expected"], "=SUM(A2:A2)")


if __name__ == "__main__":
    unittest.main()
