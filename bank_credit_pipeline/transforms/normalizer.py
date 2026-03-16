"""
Normalização de dados brutos para o schema padrão do pipeline.

Responsabilidades:
1. Converter dados do IFData (wide format) para long format padronizado
2. Converter dados da CVM para o mesmo schema
3. Garantir que cada registro tenha metadados completos de rastreabilidade

Schema padronizado (long format):
- bank_id: str          → ID do banco na tabela mestre
- bank_name: str        → Nome amigável
- ticker: str           → Ticker B3 (ou null)
- reference_date: str   → Data de referência YYYY-MM-DD
- year: int             → Ano
- quarter: int          → Trimestre (1, 2, 3, 4)
- metric_name: str      → Nome padronizado da métrica
- metric_value: float   → Valor numérico
- metric_unit: str      → Unidade (R$ mil, %, ratio)
- source_name: str      → Fonte (IFData, CVM, Calculado)
- source_table: str     → Tabela/relatório de origem
- source_field: str     → Campo original na fonte
- calculation_method: str → Fórmula usada (se calculado)
- validation_status: str → Pendente, Validado, Divergente
- notes: str            → Observações
"""

import logging
from typing import Optional

import pandas as pd

from config.banks import BANKS_MASTER
from config.settings import METRIC_FIELD_MAP

logger = logging.getLogger("bank_credit_pipeline.transforms.normalizer")


# =============================================================================
# SCHEMA DO DATAFRAME PADRONIZADO
# =============================================================================

STANDARD_COLUMNS = [
    "bank_id",
    "bank_name",
    "ticker",
    "reference_date",
    "year",
    "quarter",
    "metric_name",
    "metric_value",
    "metric_unit",
    "source_name",
    "source_table",
    "source_field",
    "calculation_method",
    "validation_status",
    "institution_type_used",
    "notes",
]


def create_empty_standard_df() -> pd.DataFrame:
    """Cria DataFrame vazio com o schema padrão."""
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def period_to_date_parts(period: int) -> tuple[str, int, int]:
    """
    Converte período IFData (YYYYMM) para componentes de data.
    
    Args:
        period: Período no formato YYYYMM (ex: 202312)
    
    Returns:
        Tuple (reference_date, year, quarter)
        Ex: (\"2023-12-31\", 2023, 4)
    """
    year = period // 100
    month = period % 100

    # Mapa mês → trimestre
    quarter_map = {3: 1, 6: 2, 9: 3, 12: 4}
    quarter = quarter_map.get(month)

    if quarter is None:
        logger.warning(f"Mês {month} não é trimestral. Usando mês/30 como proxy.")
        quarter = (month - 1) // 3 + 1

    # Último dia do mês
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    reference_date = f"{year}-{month:02d}-{last_day:02d}"

    return reference_date, year, quarter


def normalize_ifdata_report(
    df_raw: pd.DataFrame,
    bank_id: str,
    periodo: int,
    report_name: str,
    metric_mappings: Optional[dict] = None,
    institution_type_used: Optional[int] = None
) -> pd.DataFrame:
    """
    Normaliza um DataFrame bruto do IFData para o schema padrão.
    
    O IFData retorna dados em formato wide (uma coluna por métrica).
    Esta função converte para long format, aplicando mapeamento de nomes.
    
    Args:
        df_raw: DataFrame bruto do IFData (uma ou mais linhas por banco)
        bank_id: ID do banco na tabela mestre
        periodo: Período YYYYMM
        report_name: Nome do relatório para rastreabilidade
        metric_mappings: Dict customizado {metric_name: search_patterns} (opcional)
    
    Returns:
        DataFrame no schema padronizado.
    """
    if df_raw.empty:
        return create_empty_standard_df()

    bank_config = BANKS_MASTER.get(bank_id)
    if bank_config is None:
        logger.error(f"Banco '{bank_id}' não encontrado na tabela mestre.")
        return create_empty_standard_df()

    reference_date, year, quarter = period_to_date_parts(periodo)

    records = []

    # Para cada coluna do DataFrame bruto, verificar se é uma métrica mapeada
    # O IFData retorna colunas como "Coluna1", "Coluna2", etc. OU com nomes descritivos
    # dependendo do formato da resposta da API.
    # 
    # Estratégia: iterar sobre as métricas que queremos e buscar no DataFrame
    # pela correspondência de nome.

    if metric_mappings is None:
        # Filtrar apenas métricas que pertencem a este relatório
        metric_mappings = {
            k: v for k, v in METRIC_FIELD_MAP.items()
            if v.get("report") == report_name
        }

    for metric_name, config in metric_mappings.items():
        search_patterns = config.get("search_patterns", [])
        found = False

        for col in df_raw.columns:
            col_upper = col.upper()
            for pattern in search_patterns:
                if pattern.upper() in col_upper:
                    # Encontrou a coluna — pegar valor da PRIMEIRA linha apenas
                    # (o relatório pode ter múltiplas linhas se houver subcategorias,
                    # e queremos o totalizador que tipicamente é a primeira)
                    value = df_raw.iloc[0][col]
                    # Converter para numérico
                    if isinstance(value, str):
                        value = value.replace(",", ".").replace(" ", "").replace("%", "")
                    try:
                        numeric_value = float(value)
                    except (ValueError, TypeError):
                        logger.debug(
                            f"Valor não numérico para {metric_name}: '{value}' "
                            f"(banco={bank_id}, período={periodo})"
                        )
                        break

                    records.append({
                        "bank_id": bank_id,
                        "bank_name": bank_config.name,
                        "ticker": bank_config.ticker or "",
                        "reference_date": reference_date,
                        "year": year,
                        "quarter": quarter,
                        "metric_name": metric_name,
                        "metric_value": numeric_value,
                        "metric_unit": config.get("unit", ""),
                        "source_name": "IFData",
                        "source_table": f"IFData_rel{config.get('report', report_name)}",
                        "source_field": col,
                        "calculation_method": "extração_direta",
                        "validation_status": "pendente",
                        "institution_type_used": institution_type_used or "",
                        "notes": "",
                    })
                    found = True
                    break  # Sair do loop de patterns se encontrou
            if found:
                break  # Sair do loop de colunas se encontrou

        if not found:
            logger.debug(
                f"Métrica '{metric_name}' não encontrada no relatório {report_name} "
                f"para banco={bank_id}, período={periodo}"
            )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


def normalize_cvm_data(
    df_cvm: pd.DataFrame,
    bank_id: str
) -> pd.DataFrame:
    """
    Normaliza dados da CVM para o schema padrão.
    
    Args:
        df_cvm: DataFrame já processado pelo CVMClient.extract_bank_metrics()
        bank_id: ID do banco na tabela mestre
    
    Returns:
        DataFrame no schema padronizado.
    """
    if df_cvm.empty:
        return create_empty_standard_df()

    bank_config = BANKS_MASTER.get(bank_id)
    if bank_config is None:
        logger.error(f"Banco '{bank_id}' não encontrado na tabela mestre.")
        return create_empty_standard_df()

    records = []

    for _, row in df_cvm.iterrows():
        ref_date = row.get("reference_date", "")

        # Extrair ano e trimestre da data de referência
        try:
            year = int(ref_date[:4])
            month = int(ref_date[5:7])
            quarter_map = {3: 1, 6: 2, 9: 3, 12: 4, 1: 1, 2: 1, 4: 2, 5: 2, 7: 3, 8: 3, 10: 4, 11: 4}
            quarter = quarter_map.get(month, (month - 1) // 3 + 1)
        except (ValueError, IndexError):
            logger.warning(f"Data de referência inválida: '{ref_date}'")
            continue

        records.append({
            "bank_id": bank_id,
            "bank_name": bank_config.name,
            "ticker": bank_config.ticker or "",
            "reference_date": ref_date,
            "year": year,
            "quarter": quarter,
            "metric_name": row.get("metric_name", ""),
            "metric_value": row.get("metric_value", 0),
            "metric_unit": row.get("metric_unit", "R$"),
            "source_name": "CVM",
            "source_table": row.get("source_table", ""),
            "source_field": row.get("source_field", ""),
            "calculation_method": "extração_direta",
            "validation_status": "pendente",
            "institution_type_used": "consolidado_cvm",
            "notes": f"doc_type={row.get('doc_type', '')}",
        })

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


def consolidate_sources(
    df_ifdata: pd.DataFrame,
    df_cvm: pd.DataFrame,
    prefer_source: str = "IFData"
) -> pd.DataFrame:
    """
    Consolida dados de múltiplas fontes, MANTENDO AMBAS para validação cruzada.
    
    Não faz deduplicação aqui — isso é intencional. O validator.py precisa
    de ambas as fontes presentes para comparar valores e marcar divergências.
    
    A deduplicação final (mantendo apenas a fonte preferencial) é feita
    na exportação (exporter.py) ao gerar o formato wide/dashboard.
    
    O campo 'notes' é marcado com a prioridade da fonte para que o validator
    e o exporter possam distinguir fonte primária de validação.
    
    Args:
        df_ifdata: DataFrame normalizado do IFData
        df_cvm: DataFrame normalizado da CVM
        prefer_source: Fonte preferencial ("IFData" ou "CVM")
    
    Returns:
        DataFrame consolidado COM todas as fontes (sem deduplicar).
    """
    if df_ifdata.empty and df_cvm.empty:
        return create_empty_standard_df()
    if df_ifdata.empty:
        return df_cvm
    if df_cvm.empty:
        return df_ifdata

    # Concatenar tudo — SEM deduplicar
    df_all = pd.concat([df_ifdata, df_cvm], ignore_index=True)

    # Contar duplicatas para log
    key_cols = ["bank_id", "year", "quarter", "metric_name"]
    dup_count = df_all.duplicated(subset=key_cols, keep=False).sum()
    overlap = dup_count // 2  # cada duplicata aparece 2 vezes

    logger.info(
        f"Consolidação: {len(df_ifdata)} IFData + {len(df_cvm)} CVM "
        f"→ {len(df_all)} registros totais ({overlap} métricas com dados em ambas as fontes)"
    )

    return df_all
