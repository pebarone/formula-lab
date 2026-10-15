# Continuação para uso de ferramentas do Excel

Esta etapa continua os pesos do adaptador de 10.000 exemplos (`20260929-183918-1387e297`). O nome da nova versão é **10.000 exemplos + 1.000 exemplos de tool usage**. O adaptador de origem permanece em sua pasta.

## Dados e treinamento

São 1.000 conversas sintéticas novas: 500 de proposta/teste/resposta, 300 de confirmação de erro/correção/novo teste, 100 de leitura/teste e 100 de escrita autorizada/teste/leitura. Cada conversa termina com a fórmula, sem substituir a resposta pelo valor calculado. As chamadas usam os mesmos esquemas e protocolo do chat. Outros 200 exemplos do treino canônico já visto pelo adaptador são repetidos intencionalmente como revisão. Portanto a etapa processa **1.200 exemplos de treino**, dos quais 1.000 são novos exemplos de ferramentas.

Há 100 conversas sintéticas separadas para validação. Treino e validação usam tabelas, valores, textos e seeds distintos. O conjunto congelado de 500 perguntas é reservado à avaliação; suas respostas não entram nas trajetórias de treinamento. A revisão de fórmulas usa somente IDs previamente selecionados no split de treino canônico.

As fórmulas e os erros são calculados pelo Microsoft Excel instalado, em bancos de planilhas sintéticas. As escritas são realizadas pela rotina nativa de alteração; leituras posteriores confirmam os resultados. As conversas são construídas a partir desses retornos, com metadados da verificação e hashes dos arquivos. O retorno de escrita confirma a revisão salva e a quantidade de alterações; os valores são obtidos por teste/leitura, e a WebUI carrega a grade separadamente.

O tokenizer Qwen recebe o template nativo com as ferramentas. Só o conteúdo e o fim das mensagens do assistente recebem supervisão, incluindo as chamadas e a fórmula final. Perguntas, instruções, esquemas e retornos do Excel são mascarados. Exemplos maiores que o limite encerram a preparação; fórmulas e conversas não são truncadas.

A receita inicial é uma época, learning rate `0.00005`, batch `1`, acumulação `16`, sequência máxima `1536` e limite do alocador de 85% da VRAM. Rank e alpha são herdados do adaptador. O otimizador e o scheduler recomeçam; os pesos aprendidos são carregados como treináveis. A validação por loss escolhe o checkpoint salvo. Esta loss é uma medida com respostas supervisionadas, não uma taxa de acerto em respostas geradas.

## Execução de 9 de outubro de 2026

A execução `20261009-155912-5a240c2e` terminou com status `completed`, usando a receita acima e a RTX 4060 Ti de 8 GB. Processou os 1.200 exemplos em uma época e os 100 de validação, em aproximadamente 44 minutos no total. A loss média de treino foi `0.032349` e a loss de validação foi `0.003459`. Não houve truncamento: o maior exemplo de ferramentas usou 1.409 tokens.

O pico alocado pelo PyTorch foi de **5,05 GiB**, com **5,34 GiB** reservados. Uma leitura dos contadores do Windows durante o treino registrou **78 MiB de memória compartilhada** no processo; esse dado é uma medição pontual, não o pico de toda a execução. A leitura foi preservada em `runs/20261009-155912-5a240c2e/gpu_memory_probe.json`.

O hash SHA-256 dos pesos de origem permaneceu `f3bc94cd4b6eaf81372f1b69936ffcd43a8e04af24fa5f432c4ebb0c8e7fec30`. O novo adaptador tem hash `a3bf020acf9eba973e8d82ac6a8aaa267c2542eca5bc6d5108b1168a50660180` e está disponível no seletor do chat com o nome solicitado. O formato das ferramentas passado ao tokenizer mantém a mesma ordem das definições do chat; uma regressão cobre a serialização dos exemplos sem alterar essa ordem de execução.

A verificação com respostas geradas usou **12 perguntas do split separado**, três por categoria, sem fornecer as mensagens de resposta ensinadas ao modelo. Todas as 12 fórmulas coincidiram com o gabarito normalizado, executaram sem erro e foram testadas com sucesso pelo modelo antes da resposta final. Houve **24 chamadas de ferramentas**; os **três erros propostos foram confirmados, corrigidos e testados novamente**. Não houve JSON com aspas extras escapadas nem falha de execução. A GPU gerou as respostas e o Microsoft Excel calculou as fórmulas.

Os resultados dessa verificação sintética estão em `runs/20261009-155912-5a240c2e/tool_validation_report.json` e `tool_validation_predictions.jsonl`. A amostra pequena não mede o ganho sobre os 79,2% históricos do adaptador de origem. A captura `design/evidence/tool-usage-adapter.jpg` confirma o novo nome selecionado no chat.

### Avaliação posterior dos 500 casos

A execução `20261009-165108-cfb7dc9c` terminou em 09/10/2026, às 20:53, após aproximadamente 4h02min. Comparou o mesmo adaptador nos 500 exemplos canônicos completos, sem ferramentas e com ferramentas do Excel. O acerto exato normalizado foi **375/500 (75,0%) sem ferramentas** e **181/500 (36,2%) com ferramentas**, diferença de **−38,8 pontos percentuais**, com três ganhos e 197 perdas. Foram registradas 799 chamadas; 85 casos esgotaram as rodadas.

O diagnóstico de execução aceitou 362/500 fórmulas finais sem ferramentas e 398/500 com ferramentas; execução aceita não comprova equivalência semântica. A geração atingiu 32,73 e 22,47 tokens/s ponderados, respectivamente. O pico do PyTorch foi 4,43 GiB alocados e 6,28 GiB reservados. Orçamentos, escopo das amostras, falhas e verificação de integridade estão no [relatório dos 500 testes](tool_usage_500_results.md). Os registros brutos foram preservados na pasta dessa avaliação; parâmetros e código não foram alterados durante o ensaio.

## Reprodução e artefatos

Na raiz do projeto:

```powershell
.venv/Scripts/python.exe -m tools.tool_usage_data
.venv/Scripts/python.exe -m tools.train_tool_usage
```

A primeira operação gera `runs/tool-usage-dataset/{train,validation}.jsonl`, `manifest.json` e os bancos Excel nativos. A segunda registra uma nova execução normal de treinamento no painel, que pode ser acompanhada e interrompida. Não execute outra operação de GPU simultaneamente.

Depois que o adaptador estiver concluído:

```powershell
.venv/Scripts/python.exe -m tools.evaluate_tool_usage --adapter-run ID_DO_NOVO_TREINO --count 12
```

Essa verificação usa perguntas de validação inéditas, sem expor ao modelo os exemplos de resposta e chamadas ensinados. O modelo gera na GPU e consulta o Excel real. `tool_validation_report.json` separa correspondência da fórmula, execução sem erro, confirmação da fórmula final e correção seguida de novo teste. A avaliação sintética não substitui a comparação nos 500 exemplos canônicos.

`selection.json` registra os IDs, hashes dos datasets, identidade do prompt de ferramentas, origem dos pesos e composição treino/revisão. `training_summary.json` guarda loss, checkpoint e memória. O system prompt atual difere do usado originalmente nos 10.000 exemplos; essa diferença fica registrada. A continuação convencional com exclusão de IDs mantém sua verificação estrita da cadeia e pode rejeitar essa diferença histórica de prompt.

## Código e verificação

- `tools/tool_usage_data.py`: geração e confirmação pelo Excel.
- `dashboard/tool_training.py`: validação dos dados e máscara dos turnos.
- `dashboard/worker.py`: reutilização do treinador existente com o preparador específico.
- `tools/train_tool_usage.py`: registro da execução com o nome solicitado.
- `tools/evaluate_tool_usage.py`: verificação de respostas geradas no conjunto separado.
- `dashboard/excel_tools.py`: confirmação compacta de escrita.
- `dashboard/static/{app,chat}.js`: nome do adaptador e composição dos exemplos no painel.

Os testes específicos cobrem o protocolo, a supervisão, a origem, a separação dos conjuntos, o tamanho das sequências e a ausência de respostas ensinadas na entrada da avaliação.

Passaram a suíte do dashboard (142 testes), as cinco verificações frontend e os 17 testes focados após o ajuste de ordem dos esquemas. A preparação completa confirmou os 1.200/100 exemplos sem carregar o modelo na GPU; treino e geração posteriores foram feitos na GPU real. A revisão independente específica desta etapa terminou sem bloqueios concretos. A suíte completa do benchmark não foi repetida; permanece registrada uma divergência anterior entre um teste legado e o system prompt ampliado.
