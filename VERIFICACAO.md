# Verificação da instalação e da janela — 15/10/2026

## Instalação

O instalador foi executado no Windows x64 em uma cópia separada do projeto, sem `.venv`, sem dados e sem resultados locais. O teste inicial removeu Python e uv do PATH e usou uma pasta vazia para o Python gerenciado. O script baixou uv 0.12.3 e CPython 3.12.13, criou o ambiente e instalou os pacotes de `uv.lock`.

Depois, a configuração completa preparou 52.203 exemplos de treino, 5.083 de validação e 500 de teste. Os três arquivos canônicos também foram comparados byte a byte com a conversão anterior. Tamanho e SHA-256 dos arquivos de origem foram conferidos pelo instalador.

O download do modelo foi executado com a revisão fixada. Os pesos já estavam no cache deste computador; não foi necessário baixá-los novamente. A verificação conferiu tokenizer, índice e todos os arquivos de pesos, e o tokenizer foi aberto sem acesso à rede.

## Testes executados

| Verificação | Resultado |
| --- | --- |
| Testes Python do benchmark | 26 aprovados |
| Testes Python do painel | 152 aprovados |
| Testes Python das ferramentas | 26 aprovados |
| Suítes JavaScript da planilha, chat, sessões, comparação e treino | 5 aprovadas |
| Compilação dos módulos com `compileall` | Aprovada |
| Baseline com `--dry-run` após preparar os dados | 500 prompts montados |
| CUDA e bfloat16 no ambiente novo | Operação real aprovada na RTX 4060 Ti de 8 GB |
| Excel COM no ambiente novo | Criação, escrita, cálculo e leitura aprovados |
| Janela em 1366 × 768 e 390 × 844 | Centro horizontal e vertical exatos; conteúdo com rolagem |

O teste do Excel usou dados sintéticos: E2 = 23,78, F2 = 38,89 e L2 = `=(E2+F2)/2`. O valor lido foi 31,335, sem erro de fórmula. Nenhuma planilha pessoal entrou nesse teste.

A repetição do instalador também passou com `UV_OFFLINE=1` e `HF_HUB_OFFLINE=1`: runtime, uv, ambiente, 62 pacotes, dados e modelo foram reaproveitados. Os testes cobrem cache completo, cache ausente, download íntegro/corrompido e a exportação dos resultados sem conversas. O ZIP publicado no GitHub foi baixado pelo comando do README; seu PowerShell, servidor, estado inicial e CSS foram conferidos em uma cópia separada.

## Reproduzir

Depois de baixar o projeto, execute:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\configurar.ps1 -ComTestes
.\.venv\Scripts\python.exe -m benchmark.run_baseline --dry-run
.\.venv\Scripts\python.exe -m compileall -q dashboard benchmark dataset tools
node dashboard/test_workbook_frontend.cjs
node dashboard/test_chat_frontend.cjs
node dashboard/test_chat_sessions.cjs
node dashboard/test_comparison_frontend.cjs
node dashboard/test_training_frontend.cjs
```

Os comandos para instalar Node.js somente para os testes estão no [README](README.md). A integração contínua também executa a instalação e os testes sem exigir GPU, Excel ou dados locais.

## Limites da verificação

Foi criado um ambiente Python novo, mas o Windows não foi reinstalado. O driver NVIDIA, o Microsoft Excel e o runtime do Visual C++ já estavam instalados. O caminho de instalação desse runtime exige uma verificação adicional em um Windows que ainda não o possua. Não foi iniciado um novo treinamento completo nem repetida a avaliação de geração dos 500 exemplos como parte da instalação.
