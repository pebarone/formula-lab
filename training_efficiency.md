# Avaliação do treinamento — 30/09/2026

O processo já usa QLoRA NF4, batch 1, SDPA, bfloat16 e gradient checkpointing. O principal desperdício identificado foi a projeção do vocabulário para posições do prompt que não contribuem para a loss. A seleção de dados também repetia exemplos na continuação. Ambos foram corrigidos.

## Evidência das execuções anteriores

GPU local: RTX 4060 Ti, 8.188 MiB, Windows/WDDM. Ambiente: torch 2.11.0+cu128, transformers 5.17.0, PEFT 0.21.0.

| Treino concluído | Exemplos | Tempo de treino | Tempo de validação | Maior reserva registrada |
| --- | ---: | ---: | ---: | ---: |
| `20260929-091722-2a7aedca` | 1.000 | 43min16s | 5min10s / 500 exemplos | 12,37 GiB |
| `20260929-130348-d1f5077b` | 5.000 | 1h25min41s | 25min20s / 4.947 exemplos | 10,38 GiB |
| `20260929-183918-1387e297` | 10.000 | 2h43min54s | 24min56s / 4.947 exemplos | 11,00 GiB |

As reservas superavam a VRAM física, compatível com a sobrealocação observada. Os eventos mediam aproximadamente 3,4 GiB alocados **depois** de cada microbatch: isso não representa o pico durante forward, loss e backward. O monitor agora também registra picos alocados e reservados.

No treino de 10.000, a velocidade da etapa de treino foi aproximadamente 1,02 exemplo/s; a validação consumiu cerca de 13% do tempo total. Usar 500 exemplos de validação reduz esse custo, mas produz uma estimativa menos abrangente: não alterei os parâmetros das execuções existentes.

## Correção e medição de memória

O vocabulário do Qwen contém cerca de 152 mil tokens. Projetar 1.024 posições gera aproximadamente 594 MiB apenas para uma matriz de logits em float32, além dos tensores e gradientes do cálculo da loss. A máscara `-100` impede a contribuição do prompt à loss, mas sozinha não evita essas projeções e alocações.

Agora o contexto inteiro continua sendo processado, mas `logits_to_keep` seleciona apenas as posições que predizem tokens supervisionados. `shift_labels` preserva o deslocamento causal, o fechamento da resposta e a máscara de padding. O Trainer continua responsável pela normalização e acumulação dos gradientes. Não foram acrescentadas dependências nem alterada a arquitetura dos adaptadores.

O alocador do PyTorch recebe um limite antes da carga do modelo: o menor valor entre 85% da VRAM e memória livre menos 512 MiB. Esse limite não abrange alocações diretas de outras bibliotecas CUDA nem outros processos. A lista Python dos exemplos tokenizados é liberada após a conversão para o dataset do Trainer, evitando mantê-la duplicada na RAM durante todo o treinamento.

Teste local com o modelo real, rank 8, batch 1, NF4, checkpointing e sequências de 1.017, 445 e 130 tokens, incluindo backward e atualização do otimizador:

| Caminho | Resultado com orçamento de 6,41 GiB |
| --- | --- |
| Projeção antiga para todo o contexto | OOM no primeiro exemplo de 1.017 tokens; tentou alocar mais 590 MiB |
| Projeção apenas da resposta | Três passos concluídos; pico alocado de **4,46 GiB**, pico reservado de **5,05 GiB** |

Essas medições não são um benchmark de velocidade entre os dois caminhos: o antigo não terminou sob o mesmo limite. Também não garantem que batches maiores, sequências de 4.096 tokens ou respostas excepcionalmente longas caibam. Um erro de memória pede redução de batch ou sequência; não há mudança silenciosa dos dados ou tentativa de repetir o treino.

## Continuação com exemplos novos

A opção **Usar apenas exemplos novos ao continuar** vem marcada. A seleção exclui a união dos IDs registrados em `selection.json` no adaptador de origem e em todos os seus ancestrais. A garantia é por ID do dataset, não por novidade semântica. Treinos independentes continuam podendo usar os mesmos exemplos. A validação pode se repetir.

O botão **Verificar dados** usa a mesma origem e filtros do treino. Se não houver quantidade suficiente, ou se um manifesto estiver ausente/incompatível, a execução é bloqueada antes de carregar o modelo. O manifesto registra a origem das exclusões e suas contagens. Desmarcar a opção permite repetir dados intencionalmente. Os arquivos canônicos não são removidos ou modificados.

Verificação real a partir de `20260929-183918-1387e297`, mantendo seed 42, máximo de 1.024 tokens e filtro de funções suspeitas desativado, como no treino original:

- Origem: 10.000 IDs de treino.
- Nova seleção: 10.000 IDs, com **zero interseção** com a origem.
- Exclusões: 10.000 IDs já usados e 307 exemplos longos encontrados na seleção.
- Distribuição nova: 3.662 fáceis, 5.185 médios e 1.153 difíceis.
- Tokens: 4.850.697 no total e 313.092 supervisionados, aproximadamente 6,45%. A atenção ainda processa todo o contexto; a otimização reduz a projeção do vocabulário.

Os registros dessa verificação estão em `runs/new-data-audit/selection.json`; a medição de GPU está em `runs/memory-audit/probe.json`.

## Validação e limites

- 64 testes Python passaram, cobrindo loss/gradientes equivalentes, normalização por tokens, exclusão de ancestrais, repetição intencional, falta de exemplos, histórico inválido e preservação da validação.
- As três verificações de comportamento da interface passaram: treino, chat e comparação; JavaScript e Python passaram nas verificações de sintaxe.
- Um treino real de continuação com quatro exemplos novos, dois de validação e acumulação 2 passou por treino, validação, checkpoint e salvamento. Pico alocado: 4,43 GiB; reservado: 5,02 GiB. O hash dos pesos do adaptador original permaneceu igual. O adaptador de teste foi salvo em pasta temporária e removido ao concluir a verificação.
- Não executei um treino completo de mais 10.000 exemplos nem a avaliação oficial de 500 testes após as mudanças. Não há uma medição de ganho de velocidade ou qualidade para esse treino completo.

Para o próximo treino, selecione o adaptador, mantenha a opção de exemplos novos marcada e confira **Verificar dados** antes de iniciar. Batch 1, sequência 1.024 e fração de VRAM 0,85 foram os parâmetros de referência da medição. Reabra o servidor e atualize a página para carregar o código novo.

## Atualização: memória na avaliação dos 500 testes

A avaliação `20260930-162236-75b14377` usou o adaptador `20260930-153441-9e488bd6` e terminou com 395/500 acertos (79%), em aproximadamente 14min28s. Durante essa execução, foi relatado uso de 5,5 GB de memória de vídeo compartilhada. O `nvidia-smi` registrou 7.532/8.188 MiB de memória dedicada ocupada na GPU. Os eventos mostravam somente aproximadamente 2,56 GiB alocados depois dos lotes; a execução antiga não registrava a reserva nem os picos.

A correção anterior limitava apenas treino e validação de loss. A avaliação por geração continuava sem limite e com lotes de até oito exemplos, independentemente do batch de treino. O limite agora também se aplica à avaliação, antes da carga do modelo. O novo campo **Batch dos 500 testes** vem em 1, pode ir até 8 e é independente do batch de treino. O planejamento do lote contabiliza os 128 tokens possíveis de resposta, e o relatório inclui orçamento, reserva e picos. A divisão de lotes após OOM acontece depois de sair do bloco de exceção, liberando as referências aos tensores da tentativa que falhou antes da nova tentativa.

Verificação real da correção com o mesmo adapter: dez testes, incluindo os oito prompts mais longos (máximo de 1.326 tokens) e dois próximos da mediana. Batch 1 e orçamento de 6,41 GiB:

- Dez respostas completas coincidiram exatamente com as da avaliação anterior.
- Pico alocado pelo PyTorch: **3,35 GiB**; pico reservado: **5,28 GiB**.
- Uma amostra dos contadores do Windows durante o teste registrou **78 MiB compartilhados** e cerca de **4,16 GiB dedicados** no processo. Essa é uma amostra, não o pico de memória compartilhada do teste nem a memória global de todos os aplicativos.
- Registro: `runs/evaluation-memory-audit/report.json` e `events.jsonl`. O resultado oficial anterior foi preservado; essa verificação parcial não o substitui.

Após essa atualização, passaram 66 testes Python, os três testes de comportamento da interface e as verificações de sintaxe. Uma avaliação completa de 500 exemplos com o novo limite e batch 1 ainda não foi executada. A CLI independente do baseline conserva seu comportamento anterior; esta correção se aplica à avaliação pelo painel.

## Parâmetros e próximos ganhos

### Baseline completo após os fixes

O novo baseline completou os **500 testes na GPU** com batch 1, revisão fixa dos pesos, geração gulosa e limite de 128 tokens. Resultado: **3/500 (0,6%)**, em aproximadamente **19min34s**. Pico alocado pelo PyTorch: **3,285 GiB**; reservado: **6,396 GiB**, dentro do orçamento de **6,409 GiB**. Não houve OOM nem fallback para CPU. Uma amostra WMI do processo registrou **78 MiB compartilhados**; isso não representa o pico de memória compartilhada.

O relatório ativo em `benchmark/results/baseline.json` agora comprova a identidade completa das entradas, ordem, receita e commit dos pesos. O relatório, checkpoint e manifesto antigos foram preservados byte a byte com nomes `*_legacy_20261001T020305Z`. Evidências: `runs/baseline-refresh-20261001T020305Z/report.json`, `verification.json`, `environment.json` e `windows_gpu_sample.json`. A primeira tentativa parcial de 81 respostas foi preservada separadamente; foi interrompida para fixar a revisão do modelo antes da execução definitiva.

Esta execução completa avalia o **modelo base**. A verificação do adaptador com o novo limite permanece a amostra de dez testes descrita acima; não substituí o resultado oficial anterior de 79% nem executei um novo treino completo de 10.000 exemplos.

### Chat no código final

Com o mesmo prompt de 49 tokens e o adaptador de 10.000 exemplos, o cache menor mediu **26,4 contra 25,2 tokens/s (+4,9%)** em três repetições de uma resposta real de nove tokens, incluindo EOS. Base e adaptador reproduziram exatamente os IDs de `model.generate` nas três repetições. O gate respeitando EOS levou 1,3 s no base e 2,0 s no adaptador, fora a carga dos pesos. A comparação isolou o cache 512 versus 1024 no mesmo código final; a taxa inclui o prefill da resposta curta. A medição anterior de +8,8% usava 128 tokens forçados além do EOS e não é uma garantia de ganho em toda conversa. Evidência: `runs/chat-eos-verification/report.json`.

O [piloto real de learning rate](experiment_results.md) compara `5e-5` e `1e-4` nos mesmos 32 exemplos de treino e 16 de validação. O pico de VRAM foi 4,46 GiB nos dois casos, e o tempo de treino foi parecido. `1e-4` teve menor eval loss, mas base e ambos os adaptadores acertaram 0/16 fórmulas. Mantive o padrão `1e-4`: essa amostra não demonstra melhoria de resposta nem economia ao mudar o learning rate.

As próximas experiências úteis são:

1. **Escolher learning rate pela validação separada.** Repetir os candidatos com centenas de exemplos, os mesmos IDs e orçamento de atualizações; comparar loss e respostas. Não escolher parâmetros pelos 500 testes oficiais. Na continuação, `5e-5` é um candidato para medir, não uma garantia contra esquecer o aprendizado anterior.
2. **Controlar o custo da validação.** Conferir a quantidade selecionada: as execuções antigas validavam 4.947 exemplos, enquanto o ponto de partida atual é 500. Isso reduz o trabalho por época e também a abrangência da medição.
3. **Preservar batch 1 até medir folga de VRAM.** Acumulação altera o batch efetivo e a frequência de atualizações; não reduz a memória de um microbatch. Se testar batch 2, ajustar acumulação para comparar o mesmo batch efetivo e conferir picos, tempo e qualidade. [Documentação de acumulação](https://huggingface.co/docs/transformers/v5.12.0/grad_accumulation).
4. **Manter checkpointing nesta GPU de 8 GB.** Ele economiza ativações mediante recomputação, com custo de tempo. Só testar sua remoção quando a sequência e o batch realmente couberem sem ele. [Documentação de checkpointing](https://huggingface.co/docs/transformers/v5.7.0/en/grad_checkpointing).
5. **Evitar ajustes que mudam o experimento sem perceber.** Reduzir sequência muda os IDs elegíveis; rank e alpha são herdados na continuação. Warmup e scheduler cosine já estão configurados. A documentação do [Trainer](https://huggingface.co/docs/transformers/main/main_classes/trainer) descreve esses controles; não acrescentei parâmetros sem medição local.

Não implementei packing de exemplos, novos kernels ou troca de otimizador neste passe. São experimentos que precisam comprovar ganho e preservar a máscara da loss e a separação dos exemplos. As correções já medidas atacam o desperdício dominante sem trocar a receita dos adaptadores.
