# Baseline de fórmulas Excel

Este harness mede o modelo base `Qwen/Qwen3-4B-Instruct-2507` no teste congelado. Ele não treina, não altera o PyTorch e não lê outro split além do arquivo passado em `--data`.

O teste oficial fica em `dataset/canonical/test.jsonl`. Esse arquivo não pode entrar no treino. A lista de ids, quando o dataset estiver fechado, fica em `dataset/FROZEN_TEST_IDS.txt`.

`benchmark/fixtures/sample_test.jsonl` só exercita o harness. Não é o teste congelado.

## Antes da GPU

Na raiz do projeto, com o teste congelado já gravado:

```bat
.\.venv\Scripts\python.exe benchmark\run_baseline.py --dry-run
```

O dry-run lê o JSONL, monta os prompts do contrato e imprime a contagem. Não importa torch, transformers nem bitsandbytes e não carrega o modelo.

Se `dataset\canonical\test.jsonl` ainda não existir, o processo sai com código 2 e diz qual arquivo faltou. Confira o harness sem esse arquivo:

```bat
.\.venv\Scripts\python.exe -m unittest benchmark.test_scoring
.\.venv\Scripts\python.exe benchmark\run_baseline.py --data benchmark\fixtures\sample_test.jsonl --dry-run
```

## Rodar o baseline

É preciso CUDA em `cuda:0`. Não há fallback para CPU. O carregamento segue `run_model.py`: NF4, `bnb_4bit_compute_dtype` bfloat16, double quant, `device_map` em `cuda:0` e `dtype` bfloat16. A geração é gulosa (`do_sample=False`), com `max_new_tokens` 128 e `pad_token_id` do tokenizer. Só os tokens novos são decodificados.

```bat
.\.venv\Scripts\python.exe benchmark\run_baseline.py --output benchmark\baseline_result.json
```

Opções:

- `--data`: JSONL de entrada. O padrão é `dataset\canonical\test.jsonl`.
- `--limit N`: usa só as primeiras N linhas, na ordem do arquivo.
- `--output`: grava o JSON de resultado. Sem este argumento, o JSON vai para a saída padrão.
- `--checkpoint`: JSONL de predições parciais e manifesto associado. A retomada exige a mesma receita e o mesmo conteúdo de entrada, na mesma ordem.
- `--dry-run`: não gera e não grava resultado.

Use o ambiente criado por `configurar.ps1`, conforme o README principal. Não altere as versões durante uma avaliação; elas fazem parte da identidade do resultado.

## Saída

No console:

```text
Testes: 500
Corretos: 280
Accuracy: 0.5600
```

O JSON tem `model`, `data`, `total`, `correct`, `accuracy` e `items`. Cada item tem `id`, `expected`, `predicted` e `match`.

Os novos relatórios incluem `evaluation_identity`, `evaluation_fingerprint` e `input_fingerprint` por item. A identidade cobre IDs, instruções, contexto, gabaritos e ordem, além do prompt e da receita de modelo/geração. Modelo e tokenizer usam a revisão fixa `cdbee75f17c01a7cc42f958dc650907174af0554`, registrada em `model_revision` e na identidade versão 2. O checkpoint registra também a posição de cada exemplo. Modificar qualquer um desses campos bloqueia o reaproveitamento das predições.

Arquivos antigos com apenas IDs e predições não comprovam as entradas utilizadas e não são retomados. Preserve-os e escolha um novo caminho `--checkpoint`; não atribua hashes do dataset atual a resultados históricos. Um relatório legado só pode ser comparado diretamente se guardar todos os campos completos de cada entrada, na mesma ordem, e a revisão comprovada do modelo.

`accuracy` é `correct / total`. A comparação é igualdade depois de normalizar, não equivalência semântica. Espaços fora de literais entre aspas somem, letras ASCII fora das aspas viram maiúsculas, a caixa ASCII dentro de aspas duplas também vira maiúscula, e aspas simples permanecem como escritas. Se a resposta tiver um bloco cercado por crases, a fórmula é procurada primeiro ali. Vazio nunca conta como acerto.
