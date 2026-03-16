"""
Exportação da base consolidada.

Gera dois formatos:
1. Long format (bank_credit_database.csv) — formato analítico completo
2. Wide format (bank_credit_wide.csv) — formato amigável para dashboard

Também gera:
3. Sumário de cobertura (coverage_summary.csv) — resumo de quais métricas existem por banco/trimestre
"""

import logging
import os

import pandas as pd

from config.settings import OUTPUT_DIR

logger = logging.getLogger("bank_credit_pipeline.exports.exporter")


def export_long_format(df: pd.DataFrame, filename: str = "bank_credit_database.csv") -> str:
    """
    Exporta base completa em formato long (uma linha por banco × trimestre × métrica).
    
    Este é o formato rastreável e auditável.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    # Ordenar para facilitar leitura
    df = df.sort_values(
        ["bank_id", "year", "quarter", "metric_name", "source_name"],
        ascending=[True, True, True, True, True]
    )

    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    logger.info(f"Base long format exportada: {filepath} ({len(df)} linhas)")
    return filepath


def export_wide_format(df: pd.DataFrame, filename: str = "bank_credit_wide.csv") -> str:
    """
    Exporta base em formato wide (bancos nas linhas, métricas nas colunas).
    
    Filtra apenas a fonte preferencial (sem duplicatas) e pivoteia.
    Este é o formato amigável para dashboard e análise rápida.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    # Remover duplicatas mantendo fonte preferencial
    priority = {"IFData": 0, "CVM": 1, "Calculado": 2}
    df_wide = df.copy()
    df_wide["_priority"] = df_wide["source_name"].map(priority).fillna(9)
    df_wide = df_wide.sort_values("_priority")
    df_wide = df_wide.drop_duplicates(
        subset=["bank_id", "year", "quarter", "metric_name"],
        keep="first"
    )

    # Excluir métricas auxiliares do wide format (ficam apenas no long para auditoria)
    # _quarterly_diff é a versão hipotética gerada quando DRE_IS_ACCUMULATED=None
    auxiliary_suffixes = ["_quarterly_diff"]
    mask_aux = df_wide["metric_name"].apply(
        lambda x: any(x.endswith(s) for s in auxiliary_suffixes)
    )
    if mask_aux.any():
        n_excluded = mask_aux.sum()
        df_wide = df_wide[~mask_aux]
        logger.info(f"Wide: excluídas {n_excluded} métricas auxiliares (_diff)")

    # Pivotar
    try:
        pivot = df_wide.pivot_table(
            index=["bank_id", "bank_name", "ticker", "year", "quarter", "reference_date"],
            columns="metric_name",
            values="metric_value",
            aggfunc="first"
        ).reset_index()

        # Achatando as colunas
        pivot.columns.name = None

        # Ordenar
        pivot = pivot.sort_values(["bank_id", "year", "quarter"])

        pivot.to_csv(filepath, index=False, encoding="utf-8-sig")
        logger.info(f"Base wide format exportada: {filepath} ({len(pivot)} linhas)")

    except Exception as e:
        logger.error(f"Erro ao gerar wide format: {type(e).__name__}: {e}")
        # Fallback: exportar long format
        df_wide.drop(columns=["_priority"]).to_csv(filepath, index=False, encoding="utf-8-sig")

    return filepath


def export_coverage_summary(df: pd.DataFrame, filename: str = "coverage_summary.csv") -> str:
    """
    Gera sumário de cobertura: quais métricas existem por banco/trimestre.
    
    Útil para identificar lacunas antes de montar o dashboard.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    # Criar tabela de presença
    coverage = df.pivot_table(
        index=["bank_id", "year", "quarter"],
        columns="metric_name",
        values="metric_value",
        aggfunc="count",
        fill_value=0
    ).reset_index()

    coverage.columns.name = None
    coverage.to_csv(filepath, index=False, encoding="utf-8-sig")
    logger.info(f"Sumário de cobertura exportado: {filepath}")
    return filepath


def export_excel(df: pd.DataFrame, filename: str = "bank_credit_database.xlsx") -> str:
    """
    Exporta para Excel com múltiplas abas.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    try:
        with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
            # Aba 1: Base completa (long)
            df_sorted = df.sort_values(["bank_id", "year", "quarter", "metric_name"])
            df_sorted.to_excel(writer, sheet_name="Base_Completa", index=False)

            # Aba 2: Wide format
            priority = {"IFData": 0, "CVM": 1, "Calculado": 2}
            df_w = df.copy()
            df_w["_p"] = df_w["source_name"].map(priority).fillna(9)
            df_w = df_w.sort_values("_p").drop_duplicates(
                subset=["bank_id", "year", "quarter", "metric_name"], keep="first"
            )
            try:
                pivot = df_w.pivot_table(
                    index=["bank_id", "bank_name", "year", "quarter"],
                    columns="metric_name",
                    values="metric_value",
                    aggfunc="first"
                ).reset_index()
                pivot.columns.name = None
                pivot.to_excel(writer, sheet_name="Dashboard_Wide", index=False)
            except Exception:
                pass

            # Aba 3: Resumo de cobertura
            coverage = df.pivot_table(
                index=["bank_id"],
                columns="metric_name",
                values="metric_value",
                aggfunc="count",
                fill_value=0
            ).reset_index()
            coverage.columns.name = None
            coverage.to_excel(writer, sheet_name="Cobertura", index=False)

            # Aba 4: Divergências
            divergent = df[df["validation_status"] == "divergente"]
            if not divergent.empty:
                divergent.to_excel(writer, sheet_name="Divergencias", index=False)

            # Aba 5: Sanity fails
            sanity_fail = df[df["validation_status"] == "sanity_fail"]
            if not sanity_fail.empty:
                sanity_fail.to_excel(writer, sheet_name="Sanity_Fail", index=False)

        logger.info(f"Excel exportado: {filepath}")

    except Exception as e:
        logger.error(f"Erro ao exportar Excel: {type(e).__name__}: {e}")

    return filepath


def export_all(df: pd.DataFrame) -> dict[str, str]:
    """
    Executa todas as exportações e retorna os caminhos dos arquivos gerados.
    """
    paths = {}
    paths["long_csv"] = export_long_format(df)
    paths["wide_csv"] = export_wide_format(df)
    paths["coverage"] = export_coverage_summary(df)
    paths["excel"] = export_excel(df)
    return paths
