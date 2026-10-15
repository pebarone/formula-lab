# Dataset canônico de fórmulas de Excel

Fonte usada: NL2Formula, artigo [arXiv:2402.14853](https://arxiv.org/abs/2402.14853), repositório público [https://github.com/timetub/NL2Formula](https://github.com/timetub/NL2Formula), commit `bd45ed38dc6bf244366ae8a262055c38217c1d54`. Data de acesso: 2026-09-28.

Arquivos originais, baixados por HTTPS e gravados em `dataset/raw/nl2formula/`:

- `https://raw.githubusercontent.com/timetub/NL2Formula/bd45ed38dc6bf244366ae8a262055c38217c1d54/dataset/train.json` (68108457 bytes)
- `https://raw.githubusercontent.com/timetub/NL2Formula/bd45ed38dc6bf244366ae8a262055c38217c1d54/dataset/valid.json` (6824305 bytes)
- `https://raw.githubusercontent.com/timetub/NL2Formula/bd45ed38dc6bf244366ae8a262055c38217c1d54/dataset/test.json` (12980400 bytes)

Os três arquivos são blobs Git normais, não ponteiros de Git LFS. O tamanho de cada download confere com o tamanho do blob na API do GitHub.

## Licença

O repositório `timetub/NL2Formula` não contém arquivo `LICENSE`. A API do GitHub, consultada em 2026-09-28, devolve `license: null`. O artigo diz que os dados experimentais e o código estão nesse repositório. Não há, portanto, uma licença única explícita do artefato NL2Formula.

O artigo (seção 3) diz que as perguntas e as fórmulas saíram de uma conversão por regras de WikiSQL e Spider, com um acréscimo manual de cálculos a partir de TAT-QA. O arquivo `LICENSE` de [https://github.com/salesforce/WikiSQL](https://github.com/salesforce/WikiSQL) é BSD-3-Clause, copyright 2017 Salesforce Research. A página [spider.md](https://github.com/Yale-LILY/yale-lily.github.io/blob/master/spider.md) declara o dataset Spider sob CC BY-SA 4.0. O JSON publicado não marca a origem de cada linha, então a licença não pode ser separada por exemplo. A licença do TAT-QA não foi relida nesta etapa e não é afirmada aqui.

## O que cada registro contém

Cada objeto da fonte tem `TableName`, `Table` e `t5Formulas`. A instrução é o campo `Question`, já presente na fonte, em inglês. Não foi escrito template nem usada uma LLM para redigir instruções. `instruction_origin` fica `source` em todas as linhas.

A fórmula ouro é `Formula2`, o campo que o loader `model_train/datasets/nl2formula/nl2formula.py` do próprio repositório emite. Nenhuma `Formula2` traz o `=` inicial; o conversor acrescenta um. O campo `Formula` é só o esboço (sketch) e não entra em `expected`.

`context_text` usa o nome da aba (`TableName`), os cabeçalhos da linha 1 e todas as linhas de dados. A fonte não traz célula alvo; nenhuma foi inventada. As tabelas têm no máximo 28 linhas e 11 colunas.

## Split

Os arquivos `train.json`, `valid.json` e `test.json` são o split oficial. A tabela 1 do artigo cita 55165, 5523 e 10111 pares. Os JSON deste commit têm 52203, 5083 e 10111 objetos em `t5Formulas`. O teste publicado coincide com o artigo. Treino e validação estão menores; as linhas ausentes não foram fabricadas.

O teste oficial tem 10111 pares, acima de 500. Cada par recebe um id estável na ordem do arquivo, `nl2f-test-000000` em diante. A lista é ordenada por esse id e as 500 primeiras vão para `dataset/canonical/test.jsonl`. As outras 9611 permanecem só no JSON cru e não entram em treino nem em validação. Os ids congelados estão em `dataset/FROZEN_TEST_IDS.txt`, na mesma ordem do JSONL.

Treino e validação oficiais foram mantidos por inteiro: 52203 e 5083 linhas. Não houve sorteio. Os nomes de tabela dos três JSON não se sobrepõem, e nenhum trio (tabela, pergunta, fórmula) do teste oficial aparece no treino. Há 36 textos de pergunta idênticos entre o teste oficial completo e o treino, sempre em tabelas diferentes.

O nível `Calculation` aparece 860 vezes no `test.json` e nenhuma vez nos arquivos de treino e validação deste commit.

## Contagem e idioma

| Arquivo | Linhas |
| --- | ---: |
| `dataset/canonical/train.jsonl` | 52203 |
| `dataset/canonical/validation.jsonl` | 5083 |
| `dataset/canonical/test.jsonl` | 500 |

Idioma das instruções: inglês, preservado da fonte.

## Reprodução

O repositório público não inclui os JSON brutos nem os JSONL canônicos. Na raiz do projeto, depois de configurar o Python, baixe a fonte fixada, confira os hashes e converta com:

```powershell
.\.venv\Scripts\python.exe -m tools.preparar_ambiente --sem-modelo
```

Para reconverter arquivos brutos que já estejam no computador, sem novo download:

```text
.\.venv\Scripts\python.exe dataset\convert_to_canonical.py
```

O script usa apenas a biblioteca padrão, lê `dataset/raw/nl2formula/` e regrava os três JSONL e `dataset/FROZEN_TEST_IDS.txt`.

## Segundo colocado

Segundo colocado, não baixado: SpreadsheetBench, https://huggingface.co/datasets/KAKA22/SpreadsheetBench, licença declarada no cartão como cc-by-sa-4.0, acesso em 2026-09-28.
O conjunto tem 912 instruções reais de fóruns de Excel e as planilhas de teste associadas.
O gabarito é a planilha resultante da manipulação, não uma fórmula ouro em texto.
Esse formato não cabe no campo expected do contrato, que precisa começar com =.
FLAME, SpreadsheetCoder e o NL2Formula do Sheetpedia ficaram de fora: os dois primeiros não trazem este trio público, e as instruções do Sheetpedia são geradas por LLM.
