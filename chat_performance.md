# Desempenho do chat — 09/10/2026

## Causas e correções

O processo residente guardava falhas de CUDA graph pelo modelo inteiro. Uma divergência em uma pergunta desativava a aceleração para as mensagens seguintes daquele adaptador, inclusive outros tamanhos de cache. Os registros antigos mostravam sessões em aproximadamente 9 tokens/s depois dessa queda.

A referência de verificação também usava cache dinâmico enquanto o grafo usava cache estático. O ensaio de 08/10 reproduziu diferenças de tokens entre essas implementações, embora grafo e geração comum com cache estático concordassem. A referência agora usa `model.generate` com o mesmo `StaticCache`, zerado antes de cada geração, preservando os endereços capturados. O limite de verificação continua em 64 tokens e para no EOS real.

As falhas ficam em um registro de até 32 entradas por identidade do modelo/adaptadores, tamanho do cache e hash dos tokens do prompt. Outras entradas podem verificar a aceleração novamente. Repetir uma entrada rejeitada preserva um grafo validado depois dela. Cancelamento não registra falha de aceleração.

O chat mostra o modo junto da velocidade e preserva esse dado ao recarregar a conversa. Ambos os modos usam a GPU. Não houve conversão dos adaptadores FP32 para BF16 nem mesclagem dos pesos.

## Medição final em CUDA

RTX 4060 Ti de 8 GB; PyTorch 2.11.0+cu128, Transformers 5.17.0 e PEFT 0.21.0. Qwen na revisão fixa `cdbee75f17c01a7cc42f958dc650907174af0554`, NF4 e cálculo base BF16. Adaptador treinado: `20260929-183918-1387e297`; recusa: `20261002-093041-d2d5ca2a`.

Mediana de duas gerações sincronizadas por condição, incluindo prefill e EOS e excluindo preparação. Pergunta explicativa em português limitada a 64 tokens, cache de 512:

| Modelo | Compatibilidade, tokens/s | Acelerado, tokens/s | Preparação, segundos |
| --- | ---: | ---: | ---: |
| Base | 12,1 | 57,2 | 6,4 |
| Treinado | 6,2 | 43,9 | 10,5 |
| Recusa | 9,6 | 54,3 | 7,8 |
| Treinado + recusa | 6,3 | 40,3 | 11,4 |

Na fórmula curta `=SUM(A1:A10)`, com nove tokens incluindo EOS, o treinado mediu 6,8 → 25,7 tokens/s. Com 50 linhas de contexto, 786 tokens de prompt e cache de 2048, mediu 6,0 → 20,1 tokens/s em uma resposta de 19 tokens. Não são taxas garantidas para outras perguntas.

As nove condições reproduziram exatamente a referência estática em duas repetições. No prompt explicativo do treinado, a sequência diferiu da referência dinâmica; otimizações de cache podem alterar decisões próximas de empate numérico. Isso não mede qualidade das respostas. A avaliação congelada de 500 exemplos não foi refeita nesta rodada.

O backend real também completou a fórmula curta nos quatro modelos com modo acelerado e a resposta da referência. Duas requisições consecutivas ao mesmo residente treinado produziram a mesma resposta: a primeira levou 16,0 s incluindo carga e preparação; a segunda levou 0,410 s no total e reutilizou os mesmos objetos de modelo e grafo, medindo 25,6 tokens/s. O pico reservado pelo PyTorch no ensaio principal foi 3,178 GiB, dentro do orçamento de 6,409 GiB. Esse indicador não mede diretamente a memória compartilhada do Windows.

Artefatos locais: `runs/chat-speed-final-20261009/report.json`, `probe.py` e respostas por modelo. A checagem de duas requisições consecutivas no mesmo residente está em `runs/chat-speed-final-20261009/resident/report.json`, com seu script em `warm_probe.py`.

## Verificação e limites

Os 25 testes de chat e os quatro programas de teste do frontend passaram; compilação Python, sintaxe JavaScript e verificação de espaços também passaram. A revisão independente com GPT-6 Luna Max, usando a skill de qualidade solicitada, não encontrou bloqueadores no diff.

A suíte Python completa passou em 112 de 113 testes. A falha em `PromptContractTests.test_system_prompt_and_user_layout` é anterior a estas correções: o `SYSTEM_PROMPT` atual contém uma frase adicional e diverge do contrato congelado. O prompt, o teste e o baseline foram preservados.

Carregar os pesos e verificar um cache pela primeira vez ainda leva tempo. A verificação cobre até 64 tokens do primeiro prompt de cada grafo; não prova equivalência de todos os tokens ou prompts futuros. Conversas extensas exigem mais trabalho de atenção. Alterar o learning rate do treino não remove esse custo de inferência.
