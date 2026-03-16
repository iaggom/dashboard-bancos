# Pipeline de Dados de Crédito Bancário — Bancos Brasileiros

## Visão Geral

Pipeline em Python para construção de base histórica trimestral de indicadores de crédito
de bancos brasileiros, usando fontes oficiais (Banco Central e CVM) como prioridade.

---

## BLOCO 1 — Diagnóstico das Fontes

### Fonte 1: IFData (Banco Central) — API OData

| Atributo | Detalhe |
|---|---|
| **URL Base** | `https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata/` |
| **Endpoints** | `IfDataCadastro` (cadastro de IFs), `IfDataValores` (dados dos relatórios), `ListaDeRelatorio` (metadados dos relatórios) |
| **Granularidade** | Por instituição financeira ou conglomerado |
| **Periodicidade** | Trimestral |
| **Defasagem** | 60 dias (mar/jun/set), 90 dias (dez) |
| **Identificador** | `CodInst` (código interno do IFData) |
| **Tipo de Instituição** | 1=Conglomerado Financeiro, 2=Conglomerado Prudencial, 3=Instituição Individual, 4=Inst. Operações Câmbio |
| **Formato** | JSON, CSV, XML, HTML |
| **Dados desde** | 2000 (com variações de layout) |
| **Origem dos dados** | COSIF (contabilidade) + SCR (crédito) |

**Relatórios disponíveis no IFData (parâmetro `Relatorio`):**

Os relatórios contêm dados de contabilidade, capital, crédito e segmentação. Os principais são:
- Resumo (inclui Ativo Total, PL, Lucro Líquido, Índice de Basileia, CET1)
- Ativo
- Passivo e PL
- DRE (Demonstração de Resultado)
- Operações de Crédito (carteira por modalidade, inadimplência, provisões)
- Indicadores de Capital (Basileia, CET1, Capital Principal)

**O que vem pronto do IFData:**
- ✅ Patrimônio Líquido
- ✅ Lucro Líquido (trimestral e acumulado)
- ✅ Ativo Total
- ✅ Índice de Basileia
- ✅ CET1 / Índice de Capital Principal
- ✅ Carteira de Crédito Total
- ✅ Provisões (PCLD)
- ✅ Carteira com atraso >90 dias
- ✅ NPL 90+ (inadimplência >90 dias como %)
- ⚠️ ROE → precisa ser **calculado**
- ⚠️ Cobertura → precisa ser **calculada**
- ⚠️ Custo do risco → precisa ser **calculado**
- ❌ PDD por estágio (1/2/3) → **NÃO disponível** no IFData (é dado IFRS9 de Pilar 3)

**Limitações conhecidas:**
1. A API OData do IFData exige que se descubram os códigos exatos de cada relatório e coluna
2. Os nomes das colunas na API não são os mesmos que aparecem na interface web
3. O parâmetro `TipoInstituicao` determina se é banco individual (3) ou conglomerado (1 ou 2)
4. Para análise de crédito comparável, usa-se preferencialmente **Conglomerado Prudencial (tipo 2)**
5. Nem todos os bancos aparecem como conglomerado prudencial
6. Bancos como Nubank (Nu Holdings) podem ter dados consolidados via CVM, não via IFData diretamente

### Fonte 2: CVM — Dados Abertos (ITR e DFP)

| Atributo | Detalhe |
|---|---|
| **URL Base** | `https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/` |
| **Formatos** | CSV compactado (zip) por ano |
| **ITR** | Informações Trimestrais (1T, 2T, 3T) |
| **DFP** | Demonstrações Financeiras Padronizadas (anual/4T) |
| **Granularidade** | Por empresa (CNPJ) |
| **Histórico** | Desde 2011 |
| **Identificador** | CNPJ, CD_CVM |
| **Defasagem** | Semanal (reapresentações) |

**O que vem da CVM:**
- ✅ Lucro Líquido (DRE completa por conta contábil)
- ✅ Patrimônio Líquido (Balanço Patrimonial)
- ✅ Ativo Total
- ✅ Provisões (se identificáveis no BP/DRE)
- ⚠️ Só para bancos **listados em bolsa**
- ❌ CET1, Basileia, NPL → dados regulatórios, só no BC

**Bancos disponíveis via CVM:** Itaú, Bradesco, Santander, Banco do Brasil, BMG, Pine, Inter, Banrisul, ABC Brasil, BTG, Nubank (via Nu Holdings), Daycoval, Agibank (se listado)

### Fonte 3: RI / Pilar 3 (Fallback)

- Usado apenas para validação e preenchimento de lacunas
- Não programático — requer download manual de PDFs
- PDD por estágio (IFRS 9, estágios 1/2/3) geralmente só disponível aqui
- **Na versão 1 do pipeline, não será implementado automaticamente**

### Tabela-Resumo: Indicador × Fonte × Método

| Indicador | Fonte Primária | Método | Observação |
|---|---|---|---|
| Lucro Líquido | IFData | Extração direta | Validação via CVM (bancos listados) |
| Patrimônio Líquido | IFData | Extração direta | Validação via CVM |
| Ativo Total | IFData | Extração direta | — |
| Carteira de Crédito | IFData | Extração direta | — |
| Provisões (PCLD) | IFData | Extração direta | — |
| Índice de Basileia | IFData | Extração direta | Regulatório |
| CET1 | IFData | Extração direta | Regulatório |
| NPL 90+ | IFData | Extração direta ou cálculo | Atraso >90 / Carteira Total |
| ROE anualizado | Calculado | LL × 4 / PL médio | Run rate trimestral |
| ROE LTM | Calculado | LL 12m / PL médio 12m | Últimos 12 meses |
| Cobertura | Calculado | Provisões / Carteira atraso >90 | — |
| Custo do Risco | Calculado | Despesa PDD trim / Carteira média | Anualizado |
| PDD por estágio | Indisponível v1 | — | Requer Pilar 3 (manual) |

---

## BLOCO 2 — Arquitetura

```
bank_credit_pipeline/
├── README.md
├── requirements.txt
├── main.py                    # Orquestrador principal
├── config/
│   ├── __init__.py
│   ├── banks.py               # Tabela mestre de bancos
│   └── settings.py            # Configurações globais
├── sources/
│   ├── __init__.py
│   ├── ifdata_client.py       # Cliente API IFData (BC)
│   └── cvm_client.py          # Cliente dados abertos CVM
├── transforms/
│   ├── __init__.py
│   ├── normalizer.py          # Padronização de colunas
│   ├── calculator.py          # Cálculo de métricas derivadas
│   └── validator.py           # Validação cruzada
├── exports/
│   ├── __init__.py
│   └── exporter.py            # Exportação CSV/Excel
└── utils/
    ├── __init__.py
    └── logging_config.py      # Configuração de logging
```

**Fluxo de dados:**
1. `ifdata_client` → extrai dados brutos do IFData → DataFrame cru
2. `cvm_client` → extrai ITR/DFP da CVM → DataFrame cru
3. `normalizer` → padroniza tudo para schema único (long format)
4. `calculator` → calcula ROE, cobertura, custo do risco
5. `validator` → cruza IFData vs CVM, sinaliza divergências
6. `exporter` → gera base final consolidada

---

## BLOCO 3 — Plano de Implementação

1. **config/banks.py** — Tabela mestre dos 13 bancos
2. **config/settings.py** — Constantes e configurações
3. **utils/logging_config.py** — Logging estruturado
4. **sources/ifdata_client.py** — Cliente IFData (API OData)
5. **sources/cvm_client.py** — Cliente CVM (download CSV)
6. **transforms/normalizer.py** — Normalização para schema padrão
7. **transforms/calculator.py** — Cálculo de métricas derivadas
8. **transforms/validator.py** — Validação cruzada
9. **exports/exporter.py** — Exportação
10. **main.py** — Orquestrador

---

## BLOCO 5 — Checklist de Validação

### Antes de rodar
- [ ] Python 3.9+ instalado
- [ ] `pip install -r requirements.txt`
- [ ] Acesso à internet (APIs do BC e CVM)

### Após rodar
- [ ] Verificar `output/pipeline.log` para erros
- [ ] Conferir se todos os 13 bancos aparecem na base final
- [ ] Para cada banco, verificar cobertura temporal (trimestres presentes)
- [ ] Comparar LL e PL de 2-3 bancos listados com dados do RI
- [ ] Verificar que Basileia e CET1 estão em % (ex: 14.5, não 0.145)
- [ ] Verificar que ROE anualizado está coerente (ex: 15-25% para grandes bancos)
- [ ] Conferir que NPL 90+ está na faixa esperada (2-6% para maioria)
- [ ] Verificar coluna `source_name` preenchida em todas as linhas
- [ ] Verificar coluna `calculation_method` para métricas calculadas
- [ ] Checar `validation_status` para divergências sinalizadas
- [ ] Exportar um trimestre específico e comparar manualmente com IFData web

### Validações de integridade
- [ ] Nenhuma métrica com valor = 0 onde não deveria (LL pode ser negativo, mas PL não deveria ser 0)
- [ ] Basileia nunca abaixo de 8% (mínimo regulatório é ~10.5%)
- [ ] CET1 nunca abaixo de 4.5%
- [ ] NPL entre 0% e 100%
- [ ] Cobertura tipicamente entre 50% e 400%

---

## Fórmulas Implementadas

### ROE Anualizado (Run Rate Trimestral)
```
roe_annualized_quarter_runrate = (Lucro_Liquido_Trimestre × 4) / PL_Medio_Trimestre
PL_Medio_Trimestre = (PL_inicio_trimestre + PL_fim_trimestre) / 2
```

### ROE LTM (Last Twelve Months)
```
roe_ltm = Soma_LL_4_trimestres / Media_PL_4_trimestres
```

### NPL 90+ (se não vier pronto)
```
npl_90_plus = Carteira_Atraso_Acima_90_Dias / Carteira_Credito_Total × 100
```

### Cobertura
```
coverage_ratio = Provisoes_PCLD / Carteira_Atraso_Acima_90_Dias × 100
```

### Custo do Risco (Anualizado)
```
cost_of_risk = (Despesa_PDD_Trimestre × 4) / Carteira_Credito_Media × 100
```

---

## Como Rodar

```bash
pip install -r requirements.txt
python main.py
```

A saída será gerada em `output/`:
- `bank_credit_database.csv` — base completa em formato long
- `bank_credit_wide.csv` — base em formato wide (bancos × métricas × trimestres)
- `pipeline.log` — log completo da execução

### Parâmetros configuráveis em `config/settings.py`:
- `START_DATE` / `END_DATE` — período de extração
- `INSTITUTION_TYPE` — tipo de instituição no IFData (padrão: conglomerado prudencial)
- `OUTPUT_DIR` — diretório de saída
