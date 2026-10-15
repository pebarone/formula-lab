# Resultado dos 500 testes com e sem ferramentas

O adaptador **10.000 exemplos + 1.000 exemplos de tool usage** acertou **375/500 (75,0%) sem ferramentas** e **181/500 (36,2%) com ferramentas**, pela correspondência exata normalizada de fórmulas. Nesta configuração, disponibilizar as ferramentas reduziu essa métrica em **38,8 pontos percentuais**. A execução terminou; estes são os resultados completos.

## Execução

- Adaptador: `20261009-155912-5a240c2e`, continuado dos 10.000 exemplos.
- Avaliação: `20261009-165108-cfb7dc9c`.
- Início: **09/10/2026, 16:51:08**; término: **20:53:14**, horário de São Paulo.
- Duração: **4h 02min 05s**.
- Modelo: `Qwen/Qwen3-4B-Instruct-2507`, snapshot `cdbee75f17c01a7cc42f958dc650907174af0554`, NF4/double quant/bfloat16.
- Geração na GPU; cálculos pelo **Microsoft Excel instalado no Windows**. Todos os 1.714 turnos registraram geração `accelerated`.
- Os **500 IDs únicos do conjunto canônico inteiro** foram avaliados na ordem original, nas duas condições. Nenhum erro de preparação de planilha; nenhum caso removido da contagem.

## Qualidade e uso das ferramentas

| Medida | Sem ferramentas | Com ferramentas |
| --- | ---: | ---: |
| Fórmula igual ao gabarito normalizado | 375/500 (75,0%) | 181/500 (36,2%) |
| Fórmula final aceita no diagnóstico Excel | 362/500 (72,4%) | 398/500 (79,6%) |
| Igual ao gabarito e aceita no diagnóstico | 279/500 | 177/500 |
| Respostas sem fórmula final | 0 | 89 |
| Casos que esgotaram as rodadas | 0 | 85 |
| Chamadas registradas | 0 | 799 |

Na comparação por pergunta, **178** casos acertaram nos dois modos, **122** erraram nos dois, **3** passaram a acertar com ferramentas e **197** deixaram de acertar. Todos os 500 casos utilizaram alguma chamada no modo com ferramentas. Foram 762 chamadas `excel_test_formula` e 37 chamadas inválidas; nenhuma leitura ou escrita foi solicitada neste ensaio. Houve **350 retornos com erro**, distribuídos por **106 perguntas**; essa contagem inclui erros de protocolo, rejeições da lista segura e falhas de cálculo, não somente erros do Excel.

Execução aceita não significa resposta correta. Por exemplo, uma fórmula pode retornar zero ou consultar a coluna errada e ainda executar. Por outro lado, uma fórmula alternativa pode responder à pergunta e receber zero na métrica de correspondência exata. O ensaio não possui um verificador de equivalência semântica.

As **197 perdas** se dividem em:

- **126** fórmulas finais que executaram, mas diferiram do gabarito.
- **62** casos que esgotaram o orçamento sem entregar fórmula final.
- **5** fórmulas com aspas escapadas como `\"`, cuja execução final falhou.
- **4** outros casos: resposta sem fórmula ou outra falha final.

Exemplos verificáveis nos registros:

- `nl2f-test-000002`: substituiu `ROWS(UNIQUE(FILTER(...)))` por `COUNTIF(...)`. Contar linhas e contar títulos únicos podem produzir resultados diferentes.
- `nl2f-test-000006`: repetiu quatro chamadas rejeitadas por sintaxe e terminou sem fórmula; `000050` repetiu quatro chamadas com JSON inválido.
- `nl2f-test-000021`: quatro testes sem erro, mas nenhuma rodada restante para responder.
- `nl2f-test-000052`: a chamada testou a fórmula com aspas normais; a resposta final acrescentou barras de escape e foi rejeitada.
- `nl2f-test-000020`: respondeu `IF/AND` com “Yes/No” em vez da consulta de status indicada pelo gabarito. O Excel aceitou a fórmula, mas isso não comprova que ela responde à pergunta.

Os três ganhos exatos foram `000070`, `000084` e `000263`, cada um com uma chamada. Em `000084`, usou `MAXIFS` para pedir a temporada mais recente em vez de devolver todas as temporadas. Isso não demonstra autocorreção após erro em vários passos. Em `000263`, o teste em `H2`, célula referenciada pela própria fórmula, retornou zero; a verificação final em `AZ1` não conseguiu confirmar toda a área derramada. O ganho é de correspondência de fórmula, não de validação completa do resultado.

## Velocidade

| Medida | Sem ferramentas | Com ferramentas |
| --- | ---: | ---: |
| Tokens gerados | 14.820 | 60.296 |
| Tempo efetivo de geração acumulado | 452,86 s | 2.683,89 s |
| Tokens/s ponderados (tokens totais / tempo de geração) | **32,73** | **22,47** |
| Mediana de tokens/s por turno | 34,17 | 23,48 |
| Turnos de geração | 500 | 1.214 |
| Tempo médio por condição e pergunta | 7,29 s | 19,37 s |
| Mediana do tempo por condição e pergunta | 7,01 s | 18,05 s |

Tokens/s usa o tempo registrado pela geração, incluindo o processamento do prompt; exclui as chamadas do Excel. Os tempos por condição incluem o diagnóstico final no Excel também no modo sem ferramentas: não representam a latência de uma resposta simples no chat. A duração total inclui ainda preparação das planilhas, carregamento e gravação dos resultados. O modo com ferramentas gera mais tokens e processa instruções, esquemas e histórico maiores.

## Memória

O PyTorch registrou pico de **4,43 GiB alocados** e **6,28 GiB reservados**, com orçamento de **6,41 GiB** em uma GPU de **8,00 GiB**. Uma leitura dos contadores do Windows às 17:06:28 registrou **78 MiB compartilhados** e aproximadamente **3,11 GiB dedicados** no processo da avaliação. Essa leitura é pontual, não o pico de memória compartilhada de toda a execução. Memória reservada do PyTorch e contadores do driver são medidas diferentes.

## Protocolo e limites

A condição sem ferramentas usa geração gulosa com até **128 tokens**. A condição com ferramentas usa geração gulosa com até **512 tokens por turno**, **quatro rodadas** e **quatro chamadas**. Quatro chamadas sequenciais podem consumir todas as rodadas sem permitir resposta final. Portanto a diferença mede os modos completos, incluindo instruções e orçamentos distintos; não isola somente a presença de ferramentas.

Somente cabeçalhos e células da amostra textual visível foram materializados. Dados completos, nomes de tabelas e recursos externos ausentes do contexto não foram reconstruídos. Cada verificação final usa a cópia original, mesmo se a cópia de trabalho for alterada. O gabarito não foi enviado ao modelo nem às ferramentas.

Há gabaritos e respostas que usam funções bloqueadas pela lista segura atual; por exemplo, 52 verificações finais sem ferramentas foram rejeitadas por `SUMMARIZE`, embora a fórmula possa coincidir com o gabarito. Existem também falhas de leitura de resultados matriciais. Por isso, rejeição no diagnóstico não é automaticamente um erro de raciocínio do modelo. O relatório bruto preserva cada retorno.

O resultado histórico de **79,2%** do adaptador original utilizou outro system prompt. Compará-lo diretamente aos 75,0% atuais não isola o efeito desse treinamento. A validação sintética anterior de 12/12 também não previu o desempenho nos 500 casos canônicos.

## Verificação e arquivos

Foram conferidos os 500 IDs únicos na ordem canônica, perguntas e gabaritos, igualdade do relatório com o JSONL, pontuações recalculadas e totais de chamadas. Os hashes do dataset, adaptador, seleção, pedido de treino e cinco módulos do harness continuam iguais aos registrados no início. Os pesos do adaptador original também permanecem inalterados. Nenhum parâmetro ou código de execução foi ajustado durante a medição.

- Relatório bruto completo (`runs/20261009-165108-cfb7dc9c/tool_report.json`, arquivo local): métricas e todos os casos.
- Respostas e chamadas (`runs/20261009-165108-cfb7dc9c/tool_predictions.jsonl`, arquivo local): um registro por pergunta, preservando as duas condições.
- Análise recalculada (`runs/20261009-165108-cfb7dc9c/comparison_analysis.json`, arquivo local): tempos, integridade, ganhos, perdas e exemplos.
- Identidade inicial (`runs/20261009-165108-cfb7dc9c/measurement_identity.json`, arquivo local) e leitura de memória (`runs/20261009-165108-cfb7dc9c/gpu_memory_probe.json`, arquivo local).

Antes de outra rodada de treinamento, os resultados justificam corrigir o encerramento das rodadas, impedir testes em células que comprometem os dados usados na fórmula, revisar a leitura de matrizes e a compatibilidade dos gabaritos com o Excel, além de aumentar a diversidade dos exemplos de correção. Essas melhorias ainda não foram aplicadas neste ensaio; o resultado acima permanece preservado.
