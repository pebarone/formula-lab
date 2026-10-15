# Arquivos locais e componentes de terceiros

O repositório público distribui o código do Formula Lab, a documentação em português e os [resultados de experimentos autorizados para publicação](resultados/README.md). Ambientes Python, pesos, adaptadores, conversas, planilhas anexadas e os arquivos brutos/canônicos de terceiros ficam no computador em que a aplicação é executada.

- **NL2Formula:** os arquivos brutos e canônicos não são redistribuídos. O instalador baixa a revisão indicada em [dataset/README.md](dataset/README.md), verifica tamanho e SHA-256 e executa a conversão localmente. A fonte não declara uma licença única para o artefato. Consulte os termos das fontes antes de redistribuir os dados ou seus derivados.
- **Qwen3-4B-Instruct-2507:** o instalador obtém os pesos do repositório oficial do modelo no Hugging Face. Os pesos não são enviados ao GitHub. Consulte o cartão e a licença da revisão do modelo na fonte.
- **Tabulator:** a distribuição local da grade mantém a licença original em [tabulator.LICENSE](dashboard/static/vendor/tabulator.LICENSE). O texto legal original foi preservado no idioma em que é distribuído.
- **PyTorch e demais bibliotecas:** o ambiente instala as versões de `uv.lock` a partir dos índices oficiais ou do PyPI. As licenças de cada pacote continuam aplicáveis.
- **Microsoft Excel:** a automação usa uma instalação de desktop já licenciada pelo usuário. O projeto não distribui nem ativa o Office. Excel para a Web não oferece a interface COM usada nesta implementação.

Os relatórios históricos em Markdown descrevem medições feitas durante o desenvolvimento. `resultados/manifesto.json` relaciona as evidências disponíveis com suas cópias publicadas, que omitem contextos/perguntas, conversas, planilhas e caminhos pessoais. Caminhos `runs/...` citados nos relatórios se referem aos originais locais; as cópias para consulta ficam em `resultados/execucoes/`. Um clone novo começa sem pesos de adaptadores e sem conversas, e cria um histórico de execução próprio.
