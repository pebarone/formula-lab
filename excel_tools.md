# Ferramentas do Excel no Formula Lab

O harness usa o Microsoft Excel instalado no Windows para ler valores, escrever células e testar fórmulas em cópias de trabalho. Tabulator fornece a grade da WebUI; openpyxl apenas valida arquivos e materializa amostras, sem calcular fórmulas. O protocolo nativo do tokenizer Qwen apresenta três ferramentas: `excel_read_range`, `excel_test_formula` e `excel_write_cells`. A seleção da planilha fica vinculada à conversa.

Cada operação COM usa uma instância independente de Excel, com macros, eventos e atualização de vínculos desabilitados. Um processo separado limita a duração a 30 segundos. Um Windows Job Object limita a memória privada do Excel criado a 1 GiB e o encerra ao fechar. O processo identifica seu próprio PID e horário de criação; a rotina não encerra sessões de Excel do usuário. Fórmulas e arquivos são validados antes da execução. A lista de funções aceita operações nativas comuns, incluindo SUM, FILTER, LET e XLOOKUP, mas não funções de rede, add-ins ou código arbitrário.

O arquivo original anexado permanece intacto. Alterações são calculadas em uma cópia e promovidas sob um bloqueio da planilha, com revisão otimista para a UI. Testes usam outra cópia temporária. A prévia A1:L50 é guardada por revisão; planilhas maiores têm aviso e preservam todo o arquivo no download. A ferramenta pode ler até 600 células de outros intervalos. Resultados matriciais maiores ficam explicitamente não confirmados.

## Evidência local de 09/10/2026

- Excel 16.0, build 20430.0: SUM e FILTER calcularam 30 e 20 no primeiro smoke.
- CSV com 10 e 20: SUM e SOMA retornaram 30. Campos `007` e `=1+1` permaneceram texto; o último não foi executado como fórmula.
- `=1/0` retornou `#DIV/0!`; uma matriz sem espaço retornou `#SPILL!`; SEQUENCE de 25×25 foi marcada como resultado maior que o limite de leitura.
- A UI selecionou A3, gravou o número 5 e testou/salvou `=SUM(A1:A3)`, retornando 35 sobre 10, 20 e 5.
- O adaptador de 10 mil exemplos chamou `excel_test_formula` para SUM e respondeu 30. Em outra mensagem, recebeu `#DIV/0!` de uma fórmula e propôs a correção para SUM. O harness registrou as chamadas e não simulou resultados.

Registros locais: `runs/excel-tools-probe/`, `runs/20261009-102155-6f7f1d3f/` e `runs/20261009-102316-6b134efe/`. Captura da bancada integrada: `design/evidence/excel-tools-desktop.jpg`.

## Comparação pareada

Execução: `20261009-102621-aecad8b5`, adaptador `20260929-183918-1387e297`. GPU CUDA, modelo NF4 e LoRA FP32. Foram usados os primeiros dez exemplos canônicos, com o mesmo prompt e modelo. A condição sem ferramentas recebeu até 128 tokens novos; a condição com ferramentas recebeu até quatro rodadas de 512 tokens e quatro chamadas. Portanto o experimento compara esses modos completos, com orçamentos diferentes.

| Medida | Sem ferramentas | Com ferramentas |
|---|---:|---:|
| Correspondência exata normalizada | 10/10 | 6/10 |
| Fórmulas executadas sem erro no Excel | 9/10 | 6/10 |
| Chamadas de ferramenta | 0 | 17 |
| Exemplos que chamaram ferramentas | 0 | 10 |

Não houve novo acerto e houve quatro regressões: duas respostas devolveram o valor em vez da fórmula solicitada; duas fórmulas continham escapes inválidos. O conjunto pequeno não estima a qualidade nos 500 testes. O modo com ferramentas ainda precisa de treino voltado ao protocolo, ao uso de feedback e ao formato final da resposta. Não há evidência de melhora automática neste piloto.

O gabarito fica fora das mensagens e das ferramentas. A execução final usa dados originais, mesmo que o modelo tenha alterado a cópia de trabalho. O Excel calcula somente a amostra visível de `context_text`; isso não prova que uma fórmula esteja logicamente correta na planilha completa. O relatório preserva respostas individuais, chamadas e erros, e usa a mesma normalização textual do benchmark. O baseline original não foi sobrescrito.

Relatórios: `runs/20261009-102621-aecad8b5/tool_report.json`, `tool_predictions.jsonl` e `tool_summary.json`. A UI acompanha apenas o resumo e carrega o relatório completo ao concluir. Os 500 exemplos não foram executados nesta entrega.

## Organização e verificação

`workbooks.py` contém armazenamento, bloqueios e API pública; `excel_engine.py` valida e organiza operações; `excel_worker.py` possui o ciclo de vida COM; `excel_tools.py` valida o protocolo do modelo; `tool_evaluation.py` organiza a comparação. `workbook.js/css` implementam a bancada e `chat.js` preserva sua seleção no histórico. As rotas existentes mantêm a proteção de origem e token local.

A revisão Thermo-Nuclear foi realizada por Luna Max e os problemas encontrados foram corrigidos: importação numérica de CSV, limpeza de criação malsucedida, custo de chamadas COM na prévia, posição da grade, resultados matriciais incompletos e limites do processo de Excel. Testes cobrem essas fronteiras, o protocolo, cancelamento, revisões, rotas e persistência da interface. Os 137 testes do dashboard e cinco programas de teste JavaScript passam; `git diff --check` também passou. Um teste preexistente do benchmark continua falhando porque o usuário ampliou o system prompt; a instrução e o teste congelado foram preservados.
