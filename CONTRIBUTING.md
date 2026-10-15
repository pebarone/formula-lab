# Como contribuir

O ambiente suportado é Windows de 64 bits com Python 3.12. Comece pelo [README](README.md) e configure o ambiente com `configurar.ps1`.

## Verificações antes de enviar alterações

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\configurar.ps1 -SomenteVerificar -SemModelo -SemDados -SemVerificarGPU -ComTestes
node dashboard/test_workbook_frontend.cjs
node dashboard/test_chat_frontend.cjs
node dashboard/test_chat_sessions.cjs
node dashboard/test_comparison_frontend.cjs
node dashboard/test_training_frontend.cjs
git diff --check
```

Os testes unitários usam substitutos para modelo, CUDA e Excel; não iniciam treinamento. Os testes JavaScript precisam de Node.js 22 ou posterior. Os testes de integração com a GPU e o Excel devem usar cópias de planilhas e registrar o escopo da verificação.

## Dependências

Altere `pyproject.toml`, execute `uv lock` e teste a instalação em outro ambiente com `configurar.ps1 -Ambiente .venv-teste -SemModelo -SemDados -SemVerificarGPU -ComTestes`. Inclua `uv.lock` no mesmo envio. Não reinstale pacotes na `.venv` de uma execução ativa.

## Publicação

Não envie `runs/`, arquivos de credenciais, planilhas pessoais, pesos, ambientes virtuais nem dados brutos ou canônicos. A publicação inicial usa um histórico limpo porque o histórico de desenvolvimento local continha dados de terceiros. A documentação da aplicação deve permanecer em português; identificadores de API, trechos de código e licenças originais de terceiros podem conservar seu idioma.
