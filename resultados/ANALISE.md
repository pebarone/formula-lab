# Análise e explicações dos resultados

## Como interpretar

Uma fórmula conta como acerto quando coincide com o gabarito após a normalização de [contrato_baseline.md](../contrato_baseline.md). Uma fórmula diferente pode funcionar e receber zero; uma fórmula que o Excel executa pode consultar a coluna errada e também não responder à pergunta. Assim, acerto exato, execução no Excel e loss medem coisas diferentes.

Os números abaixo vêm dos arquivos versionados em [execucoes](execucoes). **Observação** descreve o que foi registrado. **Interpretação** explica o mecanismo compatível com a evidência. Quando não houve experimento que isolasse uma causa, a explicação é indicada como hipótese. Os ensaios principais usaram uma seed e não medem a variação entre repetições de treinamento.

## Modelo base: 3/500, ou 0,6%

Fontes: avaliações [9081282d](execucoes/20260929-103218-9081282d/report.json) e [baseline regenerado](execucoes/baseline-refresh-20261001T020305Z/report.json).

**Observação:** 497 respostas divergiram do gabarito. A execução posterior, com revisão fixa, batch 1 e limite de 128 tokens, repetiu os três acertos e terminou sem OOM; seu pico reservado foi 6,396 GiB.

**Interpretação:** isso mede a aderência do Qwen original ao formato e às fórmulas deste conjunto específico. A falta de adaptação aos padrões do NL2Formula é uma hipótese compatível com a melhora dos adaptadores. O número não demonstra que o modelo seja incapaz de escrever fórmulas em geral: a métrica rejeita alternativas e não verifica equivalência semântica. A repetição do total com outro controle de memória também não prova que todas as respostas foram idênticas; os arquivos por ID permitem essa conferência.

## 1.000 exemplos: 246/500, ou 49,2%

Fontes: treino [2a7aedca](execucoes/20260929-091722-2a7aedca/training_summary.json), [seleção](execucoes/20260929-091722-2a7aedca/selection.json) e avaliação [a1144f0e](execucoes/20260929-101108-a1144f0e/report.json).

**Observação:** uma época, taxa 0,0002, 1.000 exemplos de 959 tabelas e 31.129 tokens supervisionados. A loss média foi 0,369609; a melhor loss de validação, 0,125547. O resultado superou o baseline em 48,6 pontos percentuais, mas ainda houve 254 divergências.

**Por que melhorou e onde falhou:** o adaptador recebeu exemplos do formato de contexto, instrução e fórmula cobrado pelo teste. A adaptação a esse padrão é uma explicação plausível para o ganho, sem uma ablação que separe aprendizagem de formato e de operações. A distribuição dos acertos mostra a lacuna: 60/188 fáceis, 186/256 médios e 0/56 difíceis. Portanto, nem mesmo o ganho inicial se distribuiu uniformemente; o rótulo de dificuldade sozinho não prevê o desempenho. Seriam necessários testes por família de fórmula e repetições com outras seeds para atribuir causas mais específicas.

## Continuação com 5.000 exemplos: 378/500, ou 75,6%

Fontes: treino [d1f5077b](execucoes/20260929-130348-d1f5077b/training_summary.json), [seleção](execucoes/20260929-130348-d1f5077b/selection.json) e avaliação [12928eeb](execucoes/20260929-145720-12928eeb/report.json).

**Observação:** a etapa começou do adaptador de 1.000 e processou 5.000 exemplos de 4.126 tabelas. A interseção dos IDs confirma que os 1.000 anteriores estão dentro desses 5.000. Foram 6.000 exposições nas duas etapas, mas 5.000 IDs distintos. Não foram 6.000 exemplos novos. A loss média ficou em 0,073201 e a melhor loss de validação em 0,048069.

**Por que o resultado cresceu:** houve mais exemplos distintos, maior cobertura de tabelas e continuação de pesos já ajustados. Os acertos passaram a 134/188 fáceis, 215/256 médios e 29/56 difíceis; a melhora alcançou todas as faixas. Esses fatores mudaram juntos, portanto não se pode atribuir os 26,4 pontos de ganho somente à quantidade de dados. A validação também passou de 500 para 4.947 exemplos; comparar diretamente as losses com a etapa anterior mistura conjuntos diferentes. Ainda restaram 122 divergências no teste.

## 10.000 exemplos a partir do base: 396/500, ou 79,2%

Fontes: treino [1387e297](execucoes/20260929-183918-1387e297/training_summary.json), [seleção](execucoes/20260929-183918-1387e297/selection.json) e avaliação [eab8fd19](execucoes/20260929-222754-eab8fd19/report.json).

**Observação:** este treino começou do modelo base, não do adaptador de 5.000. Processou 10.000 exemplos de 7.158 tabelas; os 5.000 IDs da outra etapa estão contidos nessa seleção. A loss média foi 0,110471 e a melhor loss na mesma validação de 4.947 exemplos foi 0,039406. O teste teve 138/188 acertos fáceis, 219/256 médios e 39/56 difíceis.

**Por que o ganho foi menor:** a diferença foi de 18 acertos, ou 3,6 pontos, sobre a etapa anterior. A maior cobertura é compatível com parte desse avanço, sobretudo nos difíceis, mas o ponto de partida e o histórico de otimização também diferem. Não é uma medição controlada de “dobrar dados”. A loss média de treino maior não contradiz o resultado melhor: ela inclui o começo do aprendizado desde o base, enquanto a outra etapa começou com pesos já ajustados. Restaram 104 divergências. Mais épocas ou dados podem ajudar ou aumentar sobreajuste; isso precisa ser medido em validação separada.

## Piloto de taxa de aprendizado: ambos com 0/16

Fonte: [relatório do piloto](execucoes/training-pilot-20261001T003431Z-19016/pilot_report.json) e respostas [do base](execucoes/training-pilot-20261001T003431Z-19016/validation_base.json), [5e-5](execucoes/training-pilot-20261001T003431Z-19016/validation_lr_5e_5.json) e [1e-4](execucoes/training-pilot-20261001T003431Z-19016/validation_lr_1e_4.json).

**Condição 5e-5:** loss média 2,407972 e validação 1,755455, em 34,28 segundos de treino. **Condição 1e-4:** loss média 2,069958 e validação 1,351133, em 35,06 segundos. Só a taxa mudou; os 32 exemplos de treino, 16 de validação, seed, inicialização e oito passos do otimizador foram mantidos. Base e candidatos obtiveram zero acertos exatos.

**Explicação:** a taxa maior ajustou melhor a probabilidade dos tokens supervisionados nesse recorte. Isso não garantiu uma sequência inteira igual ao gabarito na geração gulosa. O tamanho reduzido do piloto e os poucos passos são hipóteses para a ausência de ganho de geração, não uma causa isolada demonstrada. Os picos reservados de 5,00 e 5,02 GiB e os tempos próximos não sustentam ganho de memória ou velocidade por trocar a taxa. O teste oficial de 500 não foi usado para escolher o candidato. A tentativa inicial com OOM foi registrada no manifesto e repetida em processos separados; não foi contada como resultado válido de candidato.

## Piloto com ferramentas antes da continuação: 10/10 contra 6/10

Fonte: [aecad8b5](execucoes/20261009-102621-aecad8b5/tool_report.json).

**Observação:** os dez primeiros casos deram 100% sem ferramentas e 60% com, com 17 chamadas, nenhuma melhora exata e quatro regressões.

**Explicação:** acrescentar o protocolo obriga o modelo a gerar chamadas válidas, interpretar retornos e ainda entregar a fórmula. O registro inclui, por exemplo, tentativa de teste sem o `=` inicial. Isso mostra um modo de falha adicional que não existe na resposta direta. Dez casos iniciais não são uma amostra aleatória nem estimam a taxa nos 500; o resultado motivou investigar o protocolo, sem provar que ferramentas sempre prejudicam.

## Tentativa de treino de ferramentas que falhou

Fonte: [748fe5af](execucoes/20261009-154429-748fe5af/status.json).

**Causa registrada:** os comprimentos de tokens do manifesto não correspondiam ao tokenizer Qwen local. A execução parou nessa conferência, antes de produzir um adaptador concluído. É uma falha de preparação/consistência dos dados, não um resultado de zero acertos e não o OOM de um chat. A tentativa seguinte é uma execução separada; o estado de falha foi preservado.

## Continuação para ferramentas: loss baixa e 12/12 sintéticos

Fontes: treino [5a240c2e](execucoes/20261009-155912-5a240c2e/training_summary.json), [seleção](execucoes/20261009-155912-5a240c2e/selection.json) e [validação sintética](execucoes/20261009-155912-5a240c2e/tool_validation_report.json).

**Observação:** o adaptador de 10.000 recebeu 1.000 conversas sintéticas novas e 200 fórmulas já vistas, usadas como revisão. A taxa foi 5e-5 e a sequência máxima, 1.536. A loss média foi 0,032349; a melhor loss nas 100 conversas sintéticas de validação foi 0,003459. A geração em 12 casos sintéticos separados acertou os 12, usou ferramentas nos 12 e corrigiu/retestou os três casos preparados para erro. O pico alocado foi 5,050 GiB e o reservado, 5,336 GiB.

**Por que isso não previu os 500 casos:** a validação sintética compartilha famílias e modelos de trajetória com o treino, apesar de usar casos separados. Ela testa se o protocolo foi aprendido nesse domínio. O NL2Formula contém perguntas, funções e tabelas de outra distribuição. A loss também supervisiona tokens do protocolo, enquanto a métrica final exige uma fórmula inteira. Essa diferença de tarefa explica por que as medidas não são intercambiáveis; não comprova por si só sobreajuste. Para testar a hipótese de especialização excessiva, seria necessário variar a diversidade do treino e avaliar em validação real separada.

## Avaliação completa com ferramentas: 75,0% contra 36,2%

Fontes: [cfb7dc9c](execucoes/20261009-165108-cfb7dc9c/tool_report.json), [análise recalculada](execucoes/20261009-165108-cfb7dc9c/comparison_analysis.json) e [identidade da medição](execucoes/20261009-165108-cfb7dc9c/measurement_identity.json).

**Observação:** os mesmos 500 casos produziram 375 acertos sem ferramentas e 181 com. Houve três ganhos e 197 perdas, diferença de −38,8 pontos. Nenhum caso foi retirado por falha de preparação.

**O que efetivamente causou perdas registradas:** 62 das 197 regressões esgotaram as rodadas sem fórmula final; cinco terminaram com aspas escapadas de modo inválido; 126 entregaram fórmulas que executavam, mas divergiam do gabarito; quatro tiveram outras falhas finais. O mecanismo das rodadas é verificável: uma chamada pode consumir cada turno, deixando o modelo sem uma rodada para concluir. No conjunto inteiro houve 85 casos com orçamento esgotado e 89 sem fórmula final.

**Outras dificuldades observadas:** 799 chamadas geraram 350 retornos com erro, distribuídos em 106 perguntas. Houve JSON inválido, fórmulas rejeitadas pela lista segura e resultados matriciais que o diagnóstico não conseguiu ler. Alguns gabaritos também usam funções bloqueadas. Portanto, nem todo retorno com erro representa erro de raciocínio. A taxa de fórmulas aceitas pelo Excel subiu de 72,4% para 79,6%, enquanto o acerto exato caiu: conseguir executar não garante responder corretamente.

**Custo e limites da comparação:** o modo com ferramentas gerou 60.296 tokens contra 14.820 e realizou 1.214 turnos contra 500. A taxa ponderada foi 22,47 contra 32,73 tokens/s; mais contexto, processamento de retornos e tokens estão presentes ao mesmo tempo. O ensaio mede o modo completo: sem ferramentas há 128 tokens; com ferramentas, até quatro rodadas de 512. Não isola somente a ferramenta. Os 79,2% históricos do adaptador anterior usam outro prompt; a diferença para os 75,0% atuais não prova perda causada pelo treino de continuação.

**Próximos testes justificados:** reservar um turno final, melhorar a diversidade dos exemplos de correção e avaliar separadamente funções bloqueadas e leitura de matrizes. Cada alteração precisa de uma validação própria; os resultados publicados não foram recalculados como se essas melhorias já existissem.

## Extrações do adaptador de recusa

Fontes: falha [bb42748b](execucoes/20261001-102040-bb42748b/status.json), extrações [55072cc4](execucoes/20261001-102119-55072cc4/decensor_report.json) e [d2d5ca2a](execucoes/20261002-093041-d2d5ca2a/decensor_report.json).

**Primeira tentativa:** a conversão encontrou `input_ids` onde esperava um inteiro. Esse erro de representação dos tokens impediu concluir a extração; não existe nota de qualidade dessa tentativa.

**Duas tentativas concluídas:** usaram 40 prompts de cada classe, força 1,0, LoRA rank 1 e mantiveram 36 camadas. Ambas registraram componente residual máxima de aproximadamente 7,87 × 10⁻⁶. São indicadores numéricos da extração/projeção. Não medem acerto de fórmulas, segurança das respostas nem qualidade geral. Sem uma avaliação específica com e sem esse adaptador, não há base para dizer que uma dessas duas execuções foi melhor. Nenhuma delas é continuação QLoRA dos dados de Excel.

## Auditoria de memória: 6/10 e respostas preservadas

Fonte: [evaluation-memory-audit](execucoes/evaluation-memory-audit/report.json).

**Observação:** seis acertos em dez casos selecionados para incluir os contextos mais longos; as dez respostas coincidiram com a avaliação de referência. O pico alocado foi 3,348 GiB e o reservado, 5,275 GiB.

**Explicação:** batch 1 e orçamento explícito reduziram a pressão de memória nesse recorte, preservando as respostas conferidas. Não é uma nova avaliação aleatória nem um substituto para 500 testes. O manifesto cita o adaptador `20260930-153441-9e488bd6`, cuja pasta de treino não estava disponível na exportação; não foi inventada uma configuração para preencher essa ausência.

## Conclusões que os dados sustentam

Os adaptadores históricos de fórmulas melhoraram a correspondência ao NL2Formula. A continuação ensinou o protocolo em casos sintéticos, mas o modo com ferramentas ainda apresentou falhas de encerramento, formato e compatibilidade na avaliação completa. Loss, execução sem erro e acerto exato precisam continuar separados. O caminho para explicar causalmente as diferenças é repetir condições comparáveis, mudar um fator por vez e escolher configurações em validação, preservando o teste final.
