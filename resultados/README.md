# Resultados dos treinamentos e avaliações

Esta pasta versiona os resultados reais encontrados no computador de desenvolvimento: **17 execuções e 77 arquivos de métricas, parâmetros e avaliações**. Os pesos dos adaptadores não estão incluídos. Os números são históricos; publicar estes arquivos não executa novos treinos.

Leia a [análise de cada experimento](ANALISE.md) para entender o que os resultados permitem concluir, as causas observadas dos erros e as hipóteses que ainda precisam ser testadas.

## Treinos de fórmulas

Todos usaram uma época, batch 1, acumulação 16, LoRA rank 8 / alpha 16 e seed 42. A métrica final é a igualdade de fórmulas normalizadas nos mesmos 500 testes.

| Treino | Origem e exemplos da etapa | Loss média de treino | Melhor loss de validação | Avaliação histórica |
| --- | --- | ---: | ---: | ---: |
| [1.000 exemplos](execucoes/20260929-091722-2a7aedca/training_summary.json) | Modelo base; 1.000 | 0,369609 | 0,125547, em 500 exemplos | [246/500 — 49,2%](execucoes/20260929-101108-a1144f0e/report.json) |
| [5.000 exemplos](execucoes/20260929-130348-d1f5077b/training_summary.json) | Continua o primeiro; 5.000, incluindo os 1.000 anteriores | 0,073201 | 0,048069, em 4.947 exemplos | [378/500 — 75,6%](execucoes/20260929-145720-12928eeb/report.json) |
| [10.000 exemplos](execucoes/20260929-183918-1387e297/training_summary.json) | Modelo base; 10.000 | 0,110471 | 0,039406, em 4.947 exemplos | [396/500 — 79,2%](execucoes/20260929-222754-eab8fd19/report.json) |
| [Uso de ferramentas](execucoes/20261009-155912-5a240c2e/training_summary.json) | Continua os 10.000; 1.000 conversas sintéticas + 200 fórmulas de revisão | 0,032349 | 0,003459, em 100 conversas sintéticas | [375/500 — 75,0% sem ferramentas; 181/500 — 36,2% com](execucoes/20261009-165108-cfb7dc9c/tool_report.json) |

O modelo base obteve [3/500 — 0,6%](execucoes/20260929-103218-9081282d/report.json). O [baseline regenerado](execucoes/baseline-refresh-20261001T020305Z/report.json) confirmou 3/500 com registro completo da identidade e da memória. A avaliação do treino de ferramentas usa outro prompt; não se pode atribuir a diferença entre 79,2% e 75,0% somente à continuação do treino.

## Outros experimentos e falhas preservadas

| Execução | Resultado e evidência |
| --- | --- |
| [Piloto de duas taxas de aprendizado](execucoes/training-pilot-20261001T003431Z-19016/pilot_report.json) | 32 exemplos de treino; base e dois candidatos obtiveram 0/16 na validação por geração |
| [Piloto com ferramentas no adaptador de 10.000](execucoes/20261009-102621-aecad8b5/tool_report.json) | 10/10 sem e 6/10 com ferramentas; 17 chamadas |
| [Validação sintética da continuação](execucoes/20261009-155912-5a240c2e/tool_validation_report.json) | 12/12 fórmulas; três casos de correção e novo teste |
| [Tentativa de treino interrompida](execucoes/20261009-154429-748fe5af/status.json) | Incompatibilidade entre os comprimentos do manifesto e o tokenizer; não produziu resultado de qualidade |
| [Primeira extração de recusa](execucoes/20261001-102040-bb42748b/status.json) | Falha de conversão de `input_ids`; não produziu adaptador concluído |
| [Extração de recusa de 01/10](execucoes/20261001-102119-55072cc4/decensor_report.json) | LoRA rank 1, 36 camadas; não é treino de fórmulas |
| [Extração de recusa de 02/10](execucoes/20261002-093041-d2d5ca2a/decensor_report.json) | Mesmos indicadores de extração; sem avaliação de qualidade de fórmulas |
| [Auditoria de memória da avaliação](execucoes/evaluation-memory-audit/report.json) | Dez respostas preservadas, 6/10 acertos nessa amostra selecionada; não substitui os 500 testes |

## Como conferir os arquivos

- [manifesto.json](manifesto.json) lista cada execução, o caminho de origem e os SHA-256 do arquivo original e da cópia publicada.
- Cada pasta tem parâmetros, estado e versões disponíveis. Treinos concluídos possuem `training_summary.json`, seleção por IDs e `historico_metricas.jsonl` com os eventos de loss.
- Avaliações preservam os resultados por ID, fórmulas, acertos, diagnósticos e as duas condições quando houver ferramentas. Os totais foram conferidos contra os casos individuais antes da exportação.
- A cópia publicada omite perguntas e contextos do dataset, conversas, planilhas, retornos de células, rastros completos das ferramentas e caminhos pessoais. Por isso o hash publicado pode diferir do original. Os campos dos registros mantêm os nomes usados pelo programa.
- As fontes NL2Formula são descritas em [dataset/README.md](../dataset/README.md). Os 500 testes não foram usados para treinar os adaptadores. As avaliações sucessivas são um histórico de desenvolvimento, não múltiplos testes independentes.

O relatório histórico de 395/500 citado em [training_efficiency.md](../training_efficiency.md) pertence a uma pasta de execução que já não estava disponível. Ele permanece identificado como relato histórico; não foi reconstruído nem apresentado como um arquivo bruto recuperado.

Para exportar novas execuções locais, confira antes o conteúdo que será publicado e execute:

```powershell
.\.venv\Scripts\python.exe -m tools.exportar_resultados
git diff -- resultados
```

O exportador lê somente treinos, avaliações e extrações identificadas, além das auditorias listadas no código. Ele ignora conversas e não modifica os originais. Os resultados publicados são arquivos de consulta; não são importados automaticamente para `runs/` nem disponibilizam os pesos para continuar treinando.
