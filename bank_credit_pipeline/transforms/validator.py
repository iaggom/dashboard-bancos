"""
Validação cruzada e sanity checks.

Responsabilidades:
1. Comparar dados IFData vs CVM para mesma métrica/banco/período
2. Sinalizar divergências relevantes
3. Aplicar sanity checks (Basileia > 8%, PL > 0, etc.)
4. Marcar validation_status em cada registro

Níveis de validação:
- "validado_cruzado": métrica confirmada por duas fontes (divergência < 5%)
- "divergente": divergência > 5% entre fontes (requer investigação)
- "sanity_ok": passou nos sanity checks mas sem cruzamento
- "sanity_fail": falhou em sanity check (valor fora da faixa esperada)
- "pendente": sem validação possível (fonte única, sem sanity check)
"""

import logging

import pandas as pd

logger = logging.getLogger("bank_credit_pipeline.transforms.validator")


# =============================================================================
# SANITY CHECKS
# =============================================================================
# Faixas esperadas para métricas de bancos brasileiros.
# Valores fora dessas faixas são sinalizados, NÃO excluídos.
# =============================================================================

SANITY_RULES = {
    "indice_basileia": {
        "min": 8.0, "max": 50.0,
        "note_min": "Basileia abaixo do mínimo regulatório",
        "note_max": "Basileia muito alto — verificar"
    },
    "cet1": {
        "min": 4.5, "max": 40.0,
        "note_min": "CET1 abaixo do mínimo regulatório",
        "note_max": "CET1 muito alto — verificar"
    },
    "npl_90_pct": {
        "min": 0.0, "max": 50.0,
        "note_min": "NPL negativo — impossível",
        "note_max": "NPL muito alto — verificar"
    },
    "npl_90_pct_calculated": {
        "min": 0.0, "max": 50.0,
        "note_min": "NPL negativo — impossível",
        "note_max": "NPL muito alto — verificar"
    },
    "roe_annualized_quarter_runrate": {
        "min": -80.0, "max": 60.0,
        "note_min": "ROE < -80% — verificar se há erro ou evento extraordinário",
        "note_max": "ROE > 60% — verificar se DRE não está acumulada (DRE_IS_ACCUMULATED em settings.py)"
    },
    "roe_ltm": {
        "min": -80.0, "max": 60.0,
        "note_min": "ROE LTM < -80% — verificar",
        "note_max": "ROE LTM > 60% — verificar se DRE não está acumulada"
    },
    "coverage_ratio": {
        "min": 0.0, "max": 1000.0,
        "note_min": "Cobertura negativa — impossível",
        "note_max": "Cobertura > 1000% — verificar"
    },
    "cost_of_risk_annualized": {
        "min": 0.0, "max": 30.0,
        "note_min": "Custo do risco negativo — verificar",
        "note_max": "Custo do risco muito alto — verificar"
    },
}


def apply_sanity_checks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aplica sanity checks nos valores das métricas.
    
    Não remove nenhum dado — apenas marca validation_status e adiciona notas.
    
    Args:
        df: DataFrame no schema padronizado
    
    Returns:
        DataFrame com validation_status atualizado.
    """
    df = df.copy()

    for metric_name, rules in SANITY_RULES.items():
        mask = df["metric_name"] == metric_name
        if not mask.any():
            continue

        # Converter para numérico com index alinhado ao DataFrame original
        values = pd.to_numeric(df["metric_value"], errors="coerce")

        # Verificar mínimo — combinar mask com check de valor
        below_min = mask & (values < rules["min"])
        if below_min.any():
            df.loc[below_min, "validation_status"] = "sanity_fail"
            df.loc[below_min, "notes"] = df.loc[below_min, "notes"].astype(str) + f" | {rules['note_min']}"
            count = below_min.sum()
            logger.warning(
                f"Sanity check: {count} registros de '{metric_name}' "
                f"abaixo de {rules['min']}"
            )

        # Verificar máximo
        above_max = mask & (values > rules["max"])
        if above_max.any():
            df.loc[above_max, "validation_status"] = "sanity_fail"
            df.loc[above_max, "notes"] = df.loc[above_max, "notes"].astype(str) + f" | {rules['note_max']}"
            count = above_max.sum()
            logger.warning(
                f"Sanity check: {count} registros de '{metric_name}' "
                f"acima de {rules['max']}"
            )

        # Marcar os que passaram
        passed = mask & ~below_min & ~above_max & (df["validation_status"] == "pendente")
        df.loc[passed, "validation_status"] = "sanity_ok"

    # Marcar registros sem regra de sanity
    still_pending = df["validation_status"] == "pendente"
    # Não alterar — ficam como "pendente" para indicar que não há regra de sanity aplicável

    return df


def cross_validate(
    df: pd.DataFrame,
    tolerance_pct: float = 5.0
) -> pd.DataFrame:
    """
    Valida cruzamento entre fontes para mesma métrica/banco/período.
    
    Quando IFData e CVM reportam a mesma métrica, compara os valores.
    Se divergência < tolerance_pct, marca como validado_cruzado.
    Se divergência >= tolerance_pct, marca como divergente.
    
    Args:
        df: DataFrame com dados de múltiplas fontes
        tolerance_pct: Tolerância de divergência em % (default: 5%)
    
    Returns:
        DataFrame com validation_status atualizado.
    """
    df = df.copy()

    # Encontrar métricas que existem em mais de uma fonte para mesmo banco/período
    key_cols = ["bank_id", "year", "quarter", "metric_name"]

    # Contar fontes por combinação
    source_count = df.groupby(key_cols)["source_name"].nunique()
    multi_source = source_count[source_count > 1].index

    if len(multi_source) == 0:
        logger.info("Sem dados para validação cruzada (fonte única para todas as métricas)")
        return df

    for idx in multi_source:
        bank_id, year, quarter, metric_name = idx

        # Converter tipos para comparação segura (mesma correção do calculator)
        df_year = pd.to_numeric(df["year"], errors="coerce")
        df_quarter = pd.to_numeric(df["quarter"], errors="coerce")

        mask = (
            (df["bank_id"] == bank_id)
            & (df_year == int(year))
            & (df_quarter == int(quarter))
            & (df["metric_name"] == metric_name)
        )
        subset = df[mask]

        values = pd.to_numeric(subset["metric_value"], errors="coerce")
        if values.isna().all() or len(values) < 2:
            continue

        val_min = values.min()
        val_max = values.max()

        if val_max == 0 and val_min == 0:
            df.loc[mask, "validation_status"] = "validado_cruzado"
            continue

        # Calcular divergência percentual
        if val_max != 0:
            divergence = abs(val_max - val_min) / abs(val_max) * 100
        else:
            divergence = 100.0

        if divergence <= tolerance_pct:
            df.loc[mask, "validation_status"] = "validado_cruzado"
            df.loc[mask, "notes"] = (
                df.loc[mask, "notes"].astype(str) + f" | Divergência: {divergence:.1f}%"
            )
            logger.debug(
                f"Validado cruzado: {bank_id} {year}Q{quarter} {metric_name} "
                f"(divergência={divergence:.1f}%)"
            )
        else:
            df.loc[mask, "validation_status"] = "divergente"
            df.loc[mask, "notes"] = (
                df.loc[mask, "notes"].astype(str)
                + f" | DIVERGÊNCIA: {divergence:.1f}% entre fontes "
                f"(valores: {values.tolist()})"
            )
            logger.warning(
                f"DIVERGENTE: {bank_id} {year}Q{quarter} {metric_name} "
                f"divergência={divergence:.1f}% valores={values.tolist()}"
            )

    validated_count = (df["validation_status"] == "validado_cruzado").sum()
    divergent_count = (df["validation_status"] == "divergente").sum()
    logger.info(
        f"Validação cruzada: {validated_count} validados, {divergent_count} divergentes"
    )

    return df


def validate_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Executa todas as validações: sanity checks + cruzamento entre fontes.
    """
    logger.info("Iniciando validação...")
    df = apply_sanity_checks(df)
    df = cross_validate(df)

    # Resumo final
    status_counts = df["validation_status"].value_counts()
    logger.info(f"Resumo de validação:\n{status_counts.to_string()}")

    return df
