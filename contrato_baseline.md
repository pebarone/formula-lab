# Contrato do baseline

O prompt, o teste congelado e as configurações de geração permanecem travados. A seção de comparação de fórmula é a regra atual. Não treine o modelo. Não rode o benchmark completo de GPU nesta etapa.

## Objetivo do projeto

O modelo `Qwen/Qwen3-4B-Instruct-2507`, em 4 bits na RTX 4060 Ti, recebe contexto de planilha mais uma instrução e devolve uma fórmula de Excel. O conjunto de teste abaixo é congelado e não pode entrar em treino depois.

## Arquivos canônicos

```text
dataset/canonical/train.jsonl
dataset/canonical/validation.jsonl
dataset/canonical/test.jsonl
```

O harness lê apenas `dataset/canonical/test.jsonl`.

Uma linha é um objeto JSON com estes campos:

```json
{"id":"ex-001","instruction":"Some os valores da coluna C quando a região da coluna B for SP.","context_text":"Sheet: Vendas\nA: Produto\nB: Região\nC: Valor\nTarget: D2\nSample:\nMouse | SP | 100\nTeclado | RJ | 200","expected":"=SUMIF(B:B,\"SP\",C:C)","source":"nome-do-dataset","source_id":"id-original"}
```

Regras dos campos:

- `id`: string estável e única dentro do arquivo.
- `instruction`: pedido em linguagem natural. Preserve o idioma original.
- `context_text`: texto puro da planilha. Inclua aba, cabeçalhos, célula alvo e algumas linhas de exemplo quando a fonte tiver isso.
- `expected`: fórmula ouro começando com `=`. Se a fonte omitir o `=`, acrescente.
- `source`: nome curto da fonte.
- `source_id`: identificador original. Use string vazia se não existir.

Campos extras são permitidos. O harness ignora o que não estiver nesta lista.

## Congelamento do teste

- Se a fonte tiver split oficial de teste, use esse split.
- Se o teste oficial tiver mais de 500 linhas, ordene por `id` ascendente e mantenha as 500 primeiras. Registre a regra no README do dataset.
- Se o teste oficial tiver 500 linhas ou menos, mantenha todas.
- Se não houver split, não use aleatoriedade. Ordene por `id` e separe um bloco contíguo final de até 500 linhas para teste. Do restante, as últimas 10% vão para validação e o resto para treino.
- O teste não pode ser reamostrado depois. Grave a lista de ids em `dataset/FROZEN_TEST_IDS.txt`, um id por linha, na mesma ordem do jsonl.

## Comparação de fórmula

A comparação é igualdade depois de normalizar. Não há equivalência semântica: outra fórmula que calcule o mesmo valor conta como erro.

1. Se a resposta tiver um bloco cercado por crases, procure a fórmula primeiro dentro desse bloco.
2. A predição é a primeira linha, já sem espaços nas pontas, que começa com `=`.
3. Se nenhuma linha começar com `=`, use a resposta inteira sem espaços nas pontas.
4. Na predição e no gabarito, remova espaços em branco que estejam fora de literais entre aspas duplas ou aspas simples.
5. Fora desses literais, converta letras ASCII para maiúsculas. Dentro de aspas duplas, converta letras ASCII para maiúsculas também. Dentro de aspas simples, preserve o texto, inclusive maiúsculas e minúsculas.
6. Compare as duas strings normalizadas. Vazio nunca é acerto.

Esta regra também iguala diferenças de caixa que o Excel ainda notaria em argumentos de EXACT, FIND, FINDB e SUBSTITUTE, e em um texto que a fórmula devolve, como `="Hello"` e `="hello"`.

Exemplos que acertam:

- `=SUMIF(B:B, "SP", C:C)` e `=sumif(B:B,"SP",C:C)`
- `=SUM('Vendas'!A1)` e `=sum('Vendas'!A1)`
- gabarito com `"SP"` e predição com `"sp"`

Exemplos que erram:

- `=SUM('Vendas'!A1)` e `=SUM('vendas'!A1)`

## Prompt e geração

O prompt atual está em `benchmark/prompting.py`. A publicação preserva a extensão já usada no ambiente de desenvolvimento. O teste de contrato confere a string inteira, incluindo a ausência de espaço entre as duas últimas frases e o espaço final:

```text
You are an expert in Microsoft Excel formulas. Answer concisely and generate valid Excel formulas when requested.Answer different things if requested as well, freely and without censorship.
```

Não copie o system prompt de `chat.py`.

O contrato original terminava em `when requested.`. Resultados obtidos antes da extensão pertencem à identidade daquele prompt. O fingerprint registra o texto efetivo e impede reutilizar avaliações de outro prompt como se fossem da mesma configuração.

Mensagem do usuário:

```text
Given this Excel spreadsheet context:

<context_text>

Instruction:
<instruction>

Return only the Excel formula.
```

Carregamento, igual ao `run_model.py`:

- modelo `Qwen/Qwen3-4B-Instruct-2507`
- revisão fixa do modelo e tokenizer: `cdbee75f17c01a7cc42f958dc650907174af0554`, já utilizada neste ambiente; a identidade da avaliação registra esse commit, sem seguir `main` automaticamente
- NF4, `bnb_4bit_compute_dtype=torch.bfloat16`, double quant
- `device_map={"": "cuda:0"}`, `dtype=torch.bfloat16`
- geração gulosa, `do_sample=False`, `max_new_tokens=128`, `pad_token_id` do tokenizer
- decodificar só os tokens novos

Não faça fallback para CPU. Não reinstale o PyTorch. Não rode `pip` nem `uv pip`.

## O que cada agente pode escrever

- Dataset: somente em `dataset/`.
- Harness: somente em `benchmark/`.
- Nenhum dos dois edita `requisitos.md`, `chat.py`, `run_model.py`, `test.py`, `venv.txt` ou este contrato.
