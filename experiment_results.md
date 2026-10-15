# Piloto de learning rate — 30/09/2026

## Resultado

Comparei `5e-5` com o padrão `1e-4` no Qwen real, mantendo seed, IDs, inicialização LoRA, batch, sequência e épocas iguais. O learning rate `1e-4` terminou com menor loss de treino e de validação, e o custo de treino foi praticamente igual. Na geração gulosa, porém, base e os dois adaptadores acertaram **0 de 16** fórmulas pela métrica de correspondência exata normalizada. Este piloto é pequeno demais para recomendar uma mudança: cada condição teve 8 atualizações do otimizador e apenas 16 respostas de validação.

| Learning rate | Loss de treino | Melhor eval loss | Treino (`Trainer`) | Chamada de treino completa | Tokens de contexto/s | Pico alocado / reservado | Exact match na validação |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `5e-5` | 2,408 | 1,755 | 34,28 s | 51,31 s | 338,6 | 4,46 / 5,00 GiB | 0/16 (0%) |
| `1e-4` | 2,070 | 1,351 | 35,06 s | 50,96 s | 331,0 | 4,46 / 5,02 GiB | 0/16 (0%) |

A diferença de eval loss favorece `1e-4` neste recorte, mas a qualidade de resposta não separou os candidatos. O throughput e o pico de VRAM ficaram efetivamente iguais; mudar o learning rate não trouxe ganho de velocidade ou memória observável. Os tempos de geração foram 31,3 s para o modelo base, 71,2 s para `5e-5` e 66,0 s para `1e-4`; cada saída pode ter comprimento diferente, então esses tempos não medem isoladamente o custo do treino.

## Protocolo e GPU

- Modelo: `Qwen/Qwen3-4B-Instruct-2507`, snapshot local `cdbee75f17c01a7cc42f958dc650907174af0554`.
- GPU confirmada por `nvidia-smi`: NVIDIA GeForce RTX 4060 Ti, 8.188 MiB, driver 610.88. Ambiente: PyTorch `2.11.0+cu128`, CUDA `12.8`, bfloat16 suportado.
- Um produto de matrizes bfloat16 `2048×2048` foi executado e sincronizado em `cuda:0` cinco vezes; média medida de 0,000613 s. O script falha se CUDA não estiver disponível e não troca para CPU.
- Treino: 32 exemplos, uma época e 8 atualizações, batch 1, acumulação 4, sequência máxima 512, LoRA rank 8 / alpha 16, seed 42. O otimizador foi AdamW, com scheduler cosine e 1 passo de warmup. Ambos os candidatos usaram 11.605 tokens de contexto e 795 tokens supervisionados, distribuídos pelos mesmos 32 IDs e 32 tabelas.
- Validação: os mesmos 16 IDs, com 6.954 tokens de contexto e 442 supervisionados. As 16 linhas eram 10 médias e 6 fáceis. O filtro de funções suspeitas ficou ligado, como no padrão do projeto.
- Só o learning rate mudou. A seleção determinística, os hashes do treino e da validação e a impressão digital do prompt coincidiram. Hashes: treino `248319452822339bebddcd527bfc71793c672621b711732574779c3bde10bd4b`; validação `2c33b4b53dcbd7fcedd0f6f1333059ffc69cea82de8c9bab2661bf5c33939379`; prompt `c69a839b135dbc1f10fe7b1bf63c4d32958c183bad9bcd15eea4676fe11c1b10`.
- As respostas foram geradas de forma gulosa com limite de 128 tokens. A métrica compara fórmulas normalizadas por igualdade exata; fórmulas alternativas que calculam o mesmo resultado podem receber zero.
- O conjunto oficial de 500 testes não foi usado para pontuar ou escolher estes parâmetros. Não executei essa avaliação oficial como parte do piloto.

## Evidência e arquivos

Os manifestos registram a identidade do snapshot, configuração, hashes, IDs selecionados, losses, velocidade, tempos e picos de memória. Cada treino e cada geração rodaram em processo separado; todos terminaram sem fallback para CPU. As pastas do piloto não têm `request.json` e ficam fora da lista de execuções do painel.

- Manifesto e resumo comparativo (`runs/training-pilot-20261001T003431Z-19016/pilot_report.json`, arquivo local)
- Evidência da GPU/CUDA (`runs/training-pilot-20261001T003431Z-19016/gpu_evidence.json`, arquivo local)
- Respostas do modelo base (`runs/training-pilot-20261001T003431Z-19016/validation_base.json`, arquivo local)
- Respostas com learning rate 5e-5 (`runs/training-pilot-20261001T003431Z-19016/validation_lr_5e_5.json`, arquivo local)
- Respostas com learning rate 1e-4 (`runs/training-pilot-20261001T003431Z-19016/validation_lr_1e_4.json`, arquivo local)

O primeiro disparo executou `5e-5`, mas falhou ao carregar o segundo modelo no mesmo processo por falta de VRAM. Preservei os arquivos incompletos em `failed_attempts/` e repeti o piloto com contexto CUDA novo por condição; os dois treinos e as três avaliações então terminaram. Esse erro inicial não é contado como uma medição de candidato.

## Leitura prática

Não há evidência de que a troca de `1e-4` para `5e-5` acelere ou reduza o uso de VRAM. Por enquanto, mantenha `1e-4` como referência e não trate a loss menor como melhoria demonstrada de resposta. Para escolher parâmetros, o passo seguinte é repetir com centenas de exemplos de treino e validar a qualidade em um conjunto maior separado, antes de alterar o padrão do projeto.
