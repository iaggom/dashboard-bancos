"""
Configurações globais do pipeline.

Todas as constantes configuráveis ficam aqui para facilitar ajustes
sem mexer no código dos módulos.
"""

import os
from datetime import date

# =============================================================================
# PERÍODO DE EXTRAÇÃO
# =============================================================================
# Formato AnoMes do IFData: YYYYMM
# Datas-base trimestrais válidas: 03, 06, 09, 12
START_YEAR = 2020

# END_YEAR é dinâmico, sempre o ano corrente na data de execução.
END_YEAR = date.today().year

QUARTERS = [3, 6, 9, 12]

# Defasagem conservadora de publicação
IFDATA_PUBLICATION_LAG_DAYS = 90


def get_ifdata_periods() -> list[int]:
    """
    Gera períodos no formato YYYYMM para consulta ao IFData.

    Lógica:
    - Gera todas as datas-base trimestrais de START_YEAR até END_YEAR
    - Exclui períodos futuros e períodos cuja publicação provavelmente
      ainda não ocorreu
    """
    from datetime import timedelta

    today = date.today()
    cutoff = today - timedelta(days=IFDATA_PUBLICATION_LAG_DAYS)
    cutoff_period = cutoff.year * 100 + cutoff.month

    periods = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in QUARTERS:
            period = year * 100 + month
            if period <= cutoff_period:
                periods.append(period)

    next_period = _next_quarter_period(cutoff_period)
    if next_period not in periods and next_period <= today.year * 100 + today.month:
        periods.append(next_period)

    return sorted(periods)


def _next_quarter_period(period: int) -> int:
    """Retorna o próximo período trimestral após um dado YYYYMM."""
    year = period // 100
    month = period % 100
    quarter_months = [3, 6, 9, 12]
    for qm in quarter_months:
        if qm > month:
            return year * 100 + qm
    return (year + 1) * 100 + 3


# =============================================================================
# TIPO DE INSTITUIÇÃO NO IFDATA
# =============================================================================
# 1 = Conglomerado Financeiro
# 2 = Conglomerado Prudencial
# 3 = Instituição Individual
# 4 = Instituições em operações de câmbio
DEFAULT_INSTITUTION_TYPE = 2
FALLBACK_INSTITUTION_TYPE = 3

# =============================================================================
# CÓDIGOS DE RELATÓRIOS DO IFDATA
# =============================================================================
# Validados pelo probe:
# 1 = Resumo
# 2 = Ativo
# 3 = Passivo
# 4 = DRE
# 5 = Informações de Capital
# 7 = Carteira de crédito por indexador / composição
# 8 = Carteira de crédito por nível de risco da operação
#
# Decisão prática:
# - "credito" aponta para 8, pois é o melhor candidato para NPL 90+, PCLD e risco
# - "credito_indexador" fica disponível para uso futuro
IFDATA_REPORTS = {
    "resumo": "1",
    "ativo": "2",
    "passivo": "3",
    "dre": "4",
    "capital": "5",
    "credito_indexador": "7",
    "credito": "8",
}

# =============================================================================
# MAPEAMENTO DE MÉTRICAS -> CAMPOS DO IFDATA
# =============================================================================
# O probe mais novo tenta encontrar métricas de duas formas:
# 1. match por nome de coluna
# 2. match por linhas, usando Conta, Grupo, NomeColuna, DescricaoColuna
#
# Portanto, os search_patterns abaixo devem ser pensados como termos
# amplos de busca, não apenas como nomes exatos de coluna.
METRIC_FIELD_MAP = {
    "patrimonio_liquido": {
        "report": "resumo",
        "search_patterns": [
            "Patrimônio Líquido",
            "PATRIMONIO LIQUIDO",
            "Patrimonio Liquido",
        ],
        "unit": "R$ mil",
        "description": "Patrimônio Líquido da instituição",
    },
    "lucro_liquido": {
        "report": "resumo",
        "search_patterns": [
            "Lucro Líquido",
            "LUCRO LIQUIDO",
            "Resultado Líquido",
            "RESULTADO LIQUIDO",
        ],
        "unit": "R$ mil",
        "description": "Lucro líquido do período",
    },
    "ativo_total": {
        "report": "resumo",
        "search_patterns": [
            "Ativo Total",
            "ATIVO TOTAL",
        ],
        "unit": "R$ mil",
        "description": "Ativo Total",
    },
    "indice_basileia": {
        "report": "capital",
        "search_patterns": [
            "Índice de Basileia",
            "INDICE DE BASILEIA",
            "Basileia",
            "BASILÉIA",
            "BASILEIA",
        ],
        "unit": "%",
        "description": "Índice de Basileia",
    },
    "cet1": {
        "report": "capital",
        "search_patterns": [
            "Índice de Capital Principal",
            "INDICE DE CAPITAL PRINCIPAL",
            "Capital Principal",
            "CET1",
        ],
        "unit": "%",
        "description": "CET1 / Índice de Capital Principal",
    },
    "carteira_credito_total": {
        "report": "credito",
        "search_patterns": [
            "Carteira de Crédito",
            "CARTEIRA DE CREDITO",
            "Operações de Crédito",
            "OPERACOES DE CREDITO",
            "Crédito Ativo",
        ],
        "unit": "R$ mil",
        "description": "Carteira de crédito total",
    },
    "carteira_atraso_90": {
        "report": "credito",
        "search_patterns": [
            "Vencidas há mais de 90 dias",
            "VENCIDAS HA MAIS DE 90 DIAS",
            "Atraso acima de 90 dias",
            "Atraso superior a 90 dias",
            "Acima de 90 dias",
            "Mais de 90 dias",
            "> 90 dias",
        ],
        "unit": "R$ mil",
        "description": "Carteira com atraso superior a 90 dias",
    },
    "provisoes_pcld": {
        "report": "credito",
        "search_patterns": [
            "Provisão para Créditos de Liquidação Duvidosa",
            "PROVISAO PARA CREDITOS DE LIQUIDACAO DUVIDOSA",
            "PCLD",
            "PECLD",
            "Provisão para Operações de Crédito",
            "PROVISAO PARA OPERACOES DE CREDITO",
            "Perdas Estimadas com Créditos",
        ],
        "unit": "R$ mil",
        "description": "Estoque de provisão para perdas de crédito",
    },
    "npl_90_pct": {
        "report": "credito",
        "search_patterns": [
            "Inadimplência",
            "INADIMPLENCIA",
            "NPL",
            "Atraso > 90",
            "Acima de 90 dias",
            "Mais de 90 dias",
        ],
        "unit": "%",
        "description": "Índice de inadimplência acima de 90 dias, se disponível pronto",
    },
    "despesa_pdd": {
        "report": "dre",
        "search_patterns": [
            "Provisão para Créditos",
            "PROVISAO PARA CREDITOS",
            "Despesas de Provisão",
            "Despesa de PECLD",
            "PCLD",
            "PECLD",
            "Resultado de Provisão",
        ],
        "unit": "R$ mil",
        "description": "Despesa de PDD/PCLD no período",
    },
}

# =============================================================================
# DIRETÓRIOS
# =============================================================================
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache")

# =============================================================================
# API SETTINGS
# =============================================================================
IFDATA_BASE_URL = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
IFDATA_TIMEOUT = 60
IFDATA_MAX_RETRIES = 3
IFDATA_RETRY_DELAY = 5

CVM_BASE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
CVM_TIMEOUT = 120

# =============================================================================
# DRE DO IFDATA, ACUMULADA OU TRIMESTRAL?
# =============================================================================
# Na dúvida, manter None até validar com probe mais confiável.
DRE_IS_ACCUMULATED = None