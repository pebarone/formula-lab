# LLM para fórmulas de Excel

O modelo recebe o contexto de uma planilha e uma instrução, e devolve a fórmula. A comparação é entre o Qwen original e o mesmo modelo depois do QLoRA, nos mesmos 500 testes.

Modelo travado: `Qwen/Qwen3-4B-Instruct-2507`, NF4, geração gulosa, no máximo 128 tokens. O prompt travado está em `benchmark/prompting.py`.

## Feito

- GPU RTX 4060 Ti 8 GB. PyTorch `2.11.0+cu128` no `.venv`. Não reinstalar o torch por pip: isso já trocou o CUDA por uma build de CPU.
- Dataset NL2Formula (commit `bd45ed38`) em `dataset/canonical/`.
- Treino 52203, validação 5083, teste congelado 500. Os ids estão em `dataset/FROZEN_TEST_IDS.txt` e não entram no treino.
- Harness em `benchmark/run_baseline.py`.
- Painel local em `dashboard/`, aberto por `abrir_painel.bat`: preparação, treinamento QLoRA, métricas, histórico e avaliação de adaptadores. Instruções no `README.md`.

## Baseline

```text
Qwen3-4B-Instruct-2507
Testes: 500
Corretos: 3
Accuracy: 0.6%
```

Easy 3/188, medium 0/256, hard 0/56. A fórmula precisa ser a mesma depois de normalizar maiúsculas e espaços. Resultado em `benchmark/results/baseline.json`.

## Falta

1. Executar o experimento completo de QLoRA (sugestão inicial: 5.000 exemplos) pelo painel, com o mesmo modelo e prompt.
2. Avaliar o adaptador nos 500 congelados e comparar com 0,6%. O painel preserva o baseline e usa novas predições por avaliação.
3. Auditar os gabaritos e complementar futuramente a correspondência exata com avaliação por execução no Excel.
