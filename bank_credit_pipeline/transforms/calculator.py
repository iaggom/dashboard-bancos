"""
Cálculo de métricas derivadas a partir dos dados extraídos.

Responsabilidades:
1. Calcular ROE anualizado (run rate trimestral e LTM)
2. Calcular cobertura (provisões / inadimplência 90+)
3. Calcular custo do risco (despesa PDD anualizada / carteira média)
4. Calcular NPL 90+ quando não vier pronto da fonte

Princípios:
- Cada métrica calculada é documentada com fórmula exata
- Se um insumo estiver faltando, a métrica NÃO é calculada (sem inventar)
- A coluna calculation_method descreve exatamente o que foi feito
- Métricas calculadas recebem source_name = "Calculado"
"""

import logging

import pandas as pd

from transforms.normalizer import STANDARD_COLUMNS, create_empty_standard_df

logger = logging.getLogger("bank_credit_pipeline.transforms.calculator")


def _get_metric_value(
    df: pd.DataFrame,
    bank_id: str,
    year: int,
    quarter: int,
    metric_name: str,
    _cache: dict = {}
) -> float | None:
    """
    Busca valor de uma métrica na base normalizada.
    
    Retorna None se não encontrar (nunca inventa valor).
    Usa cache interno para evitar converter year/quarter milhares de vezes.
    """
    # Cache das colunas numéricas — converte UMA VEZ por DataFrame
    df_id = id(df)
    if df_id not in _cache or _cache[df_id]["len"] != len(df):
        _cache.clear()  # Limpar caches de DataFrames anteriores
        _cache[df_id] = {
            "len": len(df),
            "year": pd.to_numeric(df["year"], errors="coerce"),
            "quarter": pd.to_numeric(df["quarter"], errors="coerce"),
        }

    df_year = _cache[df_id]["year"]
    df_quarter = _cache[df_id]["quarter"]

    mask = (
        (df["bank_id"] == bank_id)
        & (df_year == int(year))
        & (df_quarter == int(quarter))
        & (df["metric_name"] == metric_name)
    )
    matches = df[mask]
    if matches.empty:
        return None
    # Se houver múltiplas fontes (IFData + CVM), pegar a primeira (tipicamente IFData)
    return float(matches.iloc[0]["metric_value"])


def _get_previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    """Retorna (year, quarter) do trimestre anterior."""
    if quarter == 1:
        return year - 1, 4
    return year, quarter - 1


def _add_calculated_record(
    records: list[dict],
    bank_id: str,
    bank_name: str,
    ticker: str,
    reference_date: str,
    year: int,
    quarter: int,
    metric_name: str,
    value: float,
    unit: str,
    method: str,
    notes: str = ""
):
    """Adiciona um registro de métrica calculada à lista."""
    records.append({
        "bank_id": bank_id,
        "bank_name": bank_name,
        "ticker": ticker,
        "reference_date": reference_date,
        "year": year,
        "quarter": quarter,
        "metric_name": metric_name,
        "metric_value": value,
        "metric_unit": unit,
        "source_name": "Calculado",
        "source_table": "",
        "source_field": "",
        "calculation_method": method,
        "validation_status": "pendente",
        "institution_type_used": "",
        "notes": notes,
    })


# =============================================================================
# ROE ANUALIZADO — RUN RATE TRIMESTRAL
# =============================================================================
# Fórmula: (Lucro_Líquido_Trimestre × 4) / PL_Médio_Trimestre
# PL_Médio = (PL_t-1 + PL_t) / 2
#
# Se PL do trimestre anterior não estiver disponível, usa PL do próprio trimestre
# e sinaliza nas notas.
# =============================================================================

def calculate_roe_quarter_runrate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula ROE anualizado por run rate trimestral.
    
    Fórmula: (LL_trim × 4) / ((PL_t-1 + PL_t) / 2) × 100
    
    Args:
        df: DataFrame no schema padronizado com lucro_liquido e patrimonio_liquido
    
    Returns:
        DataFrame com registros de roe_annualized_quarter_runrate adicionados.
    """
    records = []

    # Identificar combinações banco/trimestre disponíveis
    bank_periods = df[["bank_id", "bank_name", "ticker", "reference_date", "year", "quarter"]].drop_duplicates()

    for _, bp in bank_periods.iterrows():
        bank_id = bp["bank_id"]
        year = int(bp["year"])
        quarter = int(bp["quarter"])

        ll = _get_metric_value(df, bank_id, year, quarter, "lucro_liquido_quarterly")
        ll_source = "lucro_liquido_quarterly"
        if ll is None:
            ll = _get_metric_value(df, bank_id, year, quarter, "lucro_liquido")
            ll_source = "lucro_liquido"
        pl_current = _get_metric_value(df, bank_id, year, quarter, "patrimonio_liquido")

        if ll is None or pl_current is None:
            logger.debug(
                f"ROE run rate: dados insuficientes para {bank_id} "
                f"{year}Q{quarter} (LL={ll}, PL={pl_current})"
            )
            continue

        # Buscar PL do trimestre anterior
        prev_year, prev_quarter = _get_previous_quarter(year, quarter)
        pl_prev = _get_metric_value(df, bank_id, prev_year, prev_quarter, "patrimonio_liquido")

        if pl_prev is not None:
            pl_medio = (pl_prev + pl_current) / 2
            notes = f"PL_medio=({pl_prev:.0f}+{pl_current:.0f})/2"
        else:
            pl_medio = pl_current
            notes = f"PL_medio=PL_atual (anterior indisponível)"

        if pl_medio == 0:
            logger.warning(f"PL médio = 0 para {bank_id} {year}Q{quarter}. Skipping ROE.")
            continue

        roe = (ll * 4 / pl_medio) * 100

        _add_calculated_record(
            records, bank_id, bp["bank_name"], bp["ticker"],
            bp["reference_date"], year, quarter,
            "roe_annualized_quarter_runrate", roe, "%",
            f"(LL_trim × 4) / PL_medio × 100 = ({ll:.0f} × 4) / {pl_medio:.0f} × 100",
            notes
        )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


# =============================================================================
# ROE LTM (LAST TWELVE MONTHS)
# =============================================================================
# Fórmula: Soma(LL dos últimos 4 trimestres) / Média(PL dos últimos 4 trimestres) × 100
#
# Só calcula se todos os 4 trimestres estiverem disponíveis.
# =============================================================================

def calculate_roe_ltm(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula ROE LTM (últimos 12 meses).
    
    A fórmula depende do DRE_IS_ACCUMULATED:
    
    - True (DRE acumulada): para Q4, LL_12m = lucro_liquido do Q4 (já é 12 meses).
      Para Q1/Q2/Q3, soma o _quarterly dos últimos 4 trimestres.
    - False (DRE trimestral): soma lucro_liquido dos últimos 4 trimestres.
    - None (desconhecido): usa _quarterly (que é direto por default).
      Se for acumulado, o resultado vai estar inflado. Marcado para revisão.
    
    Denominador sempre = Média(PL dos últimos 4 trimestres).
    Só calcula se os 4 trimestres estiverem completos.
    """
    try:
        from config.settings import DRE_IS_ACCUMULATED
        mode = DRE_IS_ACCUMULATED
    except ImportError:
        mode = None

    records = []

    bank_periods = df[["bank_id", "bank_name", "ticker", "reference_date", "year", "quarter"]].drop_duplicates()

    for _, bp in bank_periods.iterrows():
        bank_id = bp["bank_id"]
        year = int(bp["year"])
        quarter = int(bp["quarter"])

        # Coletar PL dos últimos 4 trimestres
        pl_values = []
        y, q = year, quarter
        for i in range(4):
            pl_val = _get_metric_value(df, bank_id, y, q, "patrimonio_liquido")
            if pl_val is None:
                break
            pl_values.append(pl_val)
            y, q = _get_previous_quarter(y, q)

        if len(pl_values) < 4:
            logger.debug(
                f"ROE LTM: PL insuficiente para {bank_id} {year}Q{quarter} "
                f"(apenas {len(pl_values)} de 4 trimestres)"
            )
            continue

        pl_medio_12m = sum(pl_values) / len(pl_values)
        if pl_medio_12m == 0:
            logger.warning(f"PL médio 12m = 0 para {bank_id} {year}Q{quarter}. Skipping.")
            continue

        # Calcular LL 12 meses
        ll_12m = None
        method_detail = ""

        if mode is True and quarter == 4:
            # DRE acumulada + Q4: o lucro_liquido do Q4 JÁ é 12 meses
            ll_12m = _get_metric_value(df, bank_id, year, quarter, "lucro_liquido")
            method_detail = f"DRE acumulada, Q4: LL_12m = lucro_liquido Q4 = {ll_12m}"

        if ll_12m is None:
            # Somar 4 trimestres de _quarterly
            ll_values = []
            y, q = year, quarter
            for i in range(4):
                ll_val = _get_metric_value(df, bank_id, y, q, "lucro_liquido_quarterly")
                if ll_val is None:
                    ll_val = _get_metric_value(df, bank_id, y, q, "lucro_liquido")
                if ll_val is None:
                    break
                ll_values.append(ll_val)
                y, q = _get_previous_quarter(y, q)

            if len(ll_values) < 4:
                logger.debug(
                    f"ROE LTM: LL insuficiente para {bank_id} {year}Q{quarter} "
                    f"(apenas {len(ll_values)} de 4 trimestres)"
                )
                continue

            ll_12m = sum(ll_values)
            method_detail = f"Soma(LL_quarterly_4trim) = {ll_12m:.0f} ({[f'{v:.0f}' for v in ll_values]})"

        roe_ltm = (ll_12m / pl_medio_12m) * 100

        notes = ""
        if mode is None:
            notes = (
                "⚠ DRE_IS_ACCUMULATED=None. Se DRE for acumulada, este LTM está INFLADO. "
                "Configure DRE_IS_ACCUMULATED em settings.py."
            )

        _add_calculated_record(
            records, bank_id, bp["bank_name"], bp["ticker"],
            bp["reference_date"], year, quarter,
            "roe_ltm", roe_ltm, "%",
            f"LL_12m / PL_medio_12m × 100 = {ll_12m:.0f} / {pl_medio_12m:.0f} × 100. {method_detail}",
            notes
        )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


# =============================================================================
# NPL 90+ (quando não vem pronto)
# =============================================================================
# Fórmula: (Carteira_Atraso_>90 / Carteira_Total) × 100
# =============================================================================

def calculate_npl_90(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula NPL 90+ a partir da carteira em atraso e carteira total.
    
    Só calcula se AMBOS os insumos estiverem disponíveis E se o NPL
    não tiver sido extraído diretamente (evita duplicação).
    """
    records = []

    bank_periods = df[["bank_id", "bank_name", "ticker", "reference_date", "year", "quarter"]].drop_duplicates()

    for _, bp in bank_periods.iterrows():
        bank_id = bp["bank_id"]
        year = int(bp["year"])
        quarter = int(bp["quarter"])

        # Verificar se NPL já existe como extração direta
        npl_existing = _get_metric_value(df, bank_id, year, quarter, "npl_90_pct")
        if npl_existing is not None:
            continue  # Já tem valor direto, não precisa calcular

        atraso_90 = _get_metric_value(df, bank_id, year, quarter, "carteira_atraso_90")
        carteira = _get_metric_value(df, bank_id, year, quarter, "carteira_credito_total")

        if atraso_90 is None or carteira is None:
            continue

        if carteira == 0:
            logger.warning(f"Carteira total = 0 para {bank_id} {year}Q{quarter}.")
            continue

        npl = (atraso_90 / carteira) * 100

        _add_calculated_record(
            records, bank_id, bp["bank_name"], bp["ticker"],
            bp["reference_date"], year, quarter,
            "npl_90_pct_calculated", npl, "%",
            f"(Atraso>90 / Carteira_Total) × 100 = ({atraso_90:.0f} / {carteira:.0f}) × 100",
            "Calculado a partir de componentes. Verificar se é comparável com NPL oficial."
        )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


# =============================================================================
# COBERTURA
# =============================================================================
# Fórmula: (Provisões_PCLD / Carteira_Atraso_>90) × 100
# =============================================================================

def calculate_coverage(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula índice de cobertura.
    
    Fórmula: (Provisões / Carteira_Atraso_>90) × 100
    """
    records = []

    bank_periods = df[["bank_id", "bank_name", "ticker", "reference_date", "year", "quarter"]].drop_duplicates()

    for _, bp in bank_periods.iterrows():
        bank_id = bp["bank_id"]
        year = int(bp["year"])
        quarter = int(bp["quarter"])

        provisoes = _get_metric_value(df, bank_id, year, quarter, "provisoes_pcld")
        atraso_90 = _get_metric_value(df, bank_id, year, quarter, "carteira_atraso_90")

        if provisoes is None or atraso_90 is None:
            continue

        if atraso_90 == 0:
            logger.debug(f"Atraso >90 = 0 para {bank_id} {year}Q{quarter}. Cobertura indefinida.")
            continue

        # Provisões podem ser negativas (é um saldo credor)
        # Usar valor absoluto para o cálculo
        coverage = (abs(provisoes) / atraso_90) * 100

        _add_calculated_record(
            records, bank_id, bp["bank_name"], bp["ticker"],
            bp["reference_date"], year, quarter,
            "coverage_ratio", coverage, "%",
            f"|Provisões| / Atraso>90 × 100 = |{provisoes:.0f}| / {atraso_90:.0f} × 100",
            ""
        )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


# =============================================================================
# CUSTO DO RISCO
# =============================================================================
# Fórmula: (Despesa_PDD_Trimestre × 4) / Carteira_Média × 100
#
# Carteira_Média = (Carteira_t-1 + Carteira_t) / 2
# Se não tiver trimestre anterior, usa carteira do trimestre atual.
#
# NOTA: Na versão 1, pode não ser possível separar a despesa de PDD trimestral
# de forma confiável, pois a DRE do IFData pode vir acumulada no ano.
# Nesse caso, sinalizamos e deixamos para versão 2.
# =============================================================================

def calculate_cost_of_risk(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula custo do risco anualizado.
    
    Fórmula: (Despesa_PDD_Trim × 4) / Carteira_Média × 100
    """
    records = []

    bank_periods = df[["bank_id", "bank_name", "ticker", "reference_date", "year", "quarter"]].drop_duplicates()

    for _, bp in bank_periods.iterrows():
        bank_id = bp["bank_id"]
        year = int(bp["year"])
        quarter = int(bp["quarter"])

        despesa_pdd = _get_metric_value(df, bank_id, year, quarter, "despesa_pdd_quarterly")
        if despesa_pdd is None:
            despesa_pdd = _get_metric_value(df, bank_id, year, quarter, "despesa_pdd")
        carteira_current = _get_metric_value(df, bank_id, year, quarter, "carteira_credito_total")

        if despesa_pdd is None or carteira_current is None:
            continue

        # Buscar carteira do trimestre anterior
        prev_y, prev_q = _get_previous_quarter(year, quarter)
        carteira_prev = _get_metric_value(df, bank_id, prev_y, prev_q, "carteira_credito_total")

        if carteira_prev is not None:
            carteira_media = (carteira_prev + carteira_current) / 2
            notes = ""
        else:
            carteira_media = carteira_current
            notes = "Carteira média = carteira atual (anterior indisponível)"

        if carteira_media == 0:
            continue

        # despesa_pdd geralmente é negativa (despesa), usar absoluto
        cor = (abs(despesa_pdd) * 4 / carteira_media) * 100

        _add_calculated_record(
            records, bank_id, bp["bank_name"], bp["ticker"],
            bp["reference_date"], year, quarter,
            "cost_of_risk_annualized", cor, "%",
            f"|Despesa_PDD_Trim| × 4 / Carteira_Media × 100 = |{despesa_pdd:.0f}| × 4 / {carteira_media:.0f} × 100",
            notes + " | ATENÇÃO: verificar se despesa_pdd é trimestral ou acumulada no ano."
        )

    if records:
        return pd.DataFrame(records)
    return create_empty_standard_df()


# =============================================================================
# DESTRIMESTRALIZAÇÃO DE MÉTRICAS DE FLUXO (LL, DESPESA PDD)
# =============================================================================
# O IFData pode reportar métricas de DRE acumuladas no ano (jan-mar, jan-jun,
# jan-set, jan-dez). Para calcular ROE trimestral e custo do risco, precisamos
# do valor do TRIMESTRE isolado, não do acumulado.
#
# PROBLEMA FUNDAMENTAL:
# Não existe heurística puramente numérica que distinga com certeza
# "lucro acumulado" de "lucro trimestral crescente". São matematicamente
# ambíguos sem metadado externo.
#
# SOLUÇÃO:
# 1. O pipeline usa um flag configurável DRE_IS_ACCUMULATED (em settings.py)
#    que indica se a DRE do IFData é acumulada ou trimestral.
# 2. Esse flag deve ser validado pelo probe_ifdata.py: se o LL do Q4 for
#    ~4x o LL do Q1 para os grandes bancos, é acumulado.
# 3. Quando o flag é True: gera _quarterly subtraindo trimestre anterior.
# 4. Quando o flag é False: _quarterly = valor direto (sem alteração).
# 5. Quando o flag é None (desconhecido): gera AMBAS as versões
#    (_quarterly_direct e _quarterly_diff) e marca para revisão humana.
#
# O flag é por FONTE, não por banco, porque o IFData usa o mesmo formato
# para todas as instituições em um dado relatório.
#
# IMPORTANTE: a métrica original é SEMPRE preservada intacta.
# =============================================================================

FLOW_METRICS = ["lucro_liquido", "despesa_pdd"]


def dequarterize_flow_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Processa métricas de fluxo para isolar o valor trimestral.
    
    Comportamento depende de DRE_IS_ACCUMULATED (em settings.py):
    - True: DRE vem acumulada → subtrai trimestre anterior para isolar
    - False: DRE já é trimestral → copia valor direto
    - None: Desconhecido → gera AMBAS versões para revisão humana
    
    Cria novas métricas:
    - {metric}_quarterly: versão para uso nos cálculos de ROE/CoR
    - {metric}_quarterly_diff: valor subtraído (só se DRE_IS_ACCUMULATED=None)
    
    A métrica original é preservada intacta.
    """
    # Ler o flag dentro da função para respeitar mudanças em runtime
    try:
        from config.settings import DRE_IS_ACCUMULATED
        mode = DRE_IS_ACCUMULATED
    except ImportError:
        mode = None

    records = []

    df_year = pd.to_numeric(df["year"], errors="coerce")
    df_quarter = pd.to_numeric(df["quarter"], errors="coerce")

    mode = DRE_IS_ACCUMULATED
    if mode is True:
        logger.info("Destrimestralização: DRE_IS_ACCUMULATED=True → subtraindo trimestre anterior")
    elif mode is False:
        logger.info("Destrimestralização: DRE_IS_ACCUMULATED=False → usando valor direto")
    else:
        logger.warning(
            "Destrimestralização: DRE_IS_ACCUMULATED=None (não configurado). "
            "Gerando AMBAS as versões (_quarterly_direct e _quarterly_diff). "
            "ROE e CoR usarão a versão DIRETA por segurança. "
            "AÇÃO: rode probe_ifdata.py, verifique, e configure DRE_IS_ACCUMULATED em settings.py."
        )

    for flow_metric in FLOW_METRICS:
        metric_data = df[df["metric_name"] == flow_metric].copy()
        if metric_data.empty:
            continue

        # Filtrar apenas dados do IFData (CVM já é trimestral por definição do ITR)
        ifdata_data = metric_data[metric_data["source_name"] == "IFData"]
        cvm_data = metric_data[metric_data["source_name"] == "CVM"]

        # CVM: sempre trimestral (ITR é trimestral por definição)
        for _, row in cvm_data.iterrows():
            records.append(_make_quarterly_record(
                row, flow_metric, float(row["metric_value"]),
                f"{flow_metric}_quarterly: CVM = valor direto (ITR é sempre trimestral)",
                "Fonte CVM/ITR: sempre trimestral por definição."
            ))

        # IFData: depende do flag
        for bank_id in ifdata_data["bank_id"].unique():
            bank_data = ifdata_data[ifdata_data["bank_id"] == bank_id]
            bank_name = bank_data.iloc[0]["bank_name"]
            ticker = bank_data.iloc[0]["ticker"]

            for year in sorted(bank_data["year"].unique()):
                year_int = int(year)
                year_data = bank_data[df_year[bank_data.index] == year_int]

                # Coletar valores por trimestre do ano
                q_values = {}
                q_refs = {}
                for _, row in year_data.iterrows():
                    q = int(row["quarter"])
                    q_values[q] = float(row["metric_value"])
                    q_refs[q] = row["reference_date"]

                sorted_qs = sorted(q_values.keys())

                for q in sorted_qs:
                    val = q_values[q]
                    ref_date = q_refs[q]
                    base = {
                        "bank_id": bank_id, "bank_name": bank_name,
                        "ticker": ticker, "reference_date": ref_date,
                        "year": year_int, "quarter": q,
                        "metric_unit": "R$ mil", "source_name": "Calculado",
                        "source_table": "", "source_field": "",
                        "validation_status": "pendente",
                        "institution_type_used": "",
                    }

                    # Calcular valor subtraído (diff)
                    if q == sorted_qs[0]:
                        diff_val = val  # Primeiro tri: sempre igual
                        diff_note = "Primeiro trimestre do ano: direto = diff."
                    else:
                        prev_q = sorted_qs[sorted_qs.index(q) - 1]
                        prev_val = q_values[prev_q]
                        diff_val = val - prev_val
                        diff_note = f"Q{q} - Q{prev_q} = {val:.0f} - {prev_val:.0f} = {diff_val:.0f}"

                    if mode is True:
                        # Acumulado confirmado → usar diff
                        records.append({
                            **base,
                            "metric_name": f"{flow_metric}_quarterly",
                            "metric_value": diff_val,
                            "calculation_method": f"DRE acumulada confirmada. {diff_note}",
                            "notes": "DRE_IS_ACCUMULATED=True (configurado em settings.py).",
                        })

                    elif mode is False:
                        # Trimestral confirmado → usar direto
                        records.append({
                            **base,
                            "metric_name": f"{flow_metric}_quarterly",
                            "metric_value": val,
                            "calculation_method": f"DRE trimestral confirmada. Valor direto = {val:.0f}",
                            "notes": "DRE_IS_ACCUMULATED=False (configurado em settings.py).",
                        })

                    else:
                        # Desconhecido → gerar ambas + usar direto como default seguro
                        # _quarterly (usada nos cálculos): valor direto por segurança
                        records.append({
                            **base,
                            "metric_name": f"{flow_metric}_quarterly",
                            "metric_value": val,
                            "calculation_method": (
                                f"DRE_IS_ACCUMULATED=None. Usando valor direto como default seguro. "
                                f"Se a DRE for acumulada, este valor está ERRADO para Q2+. "
                                f"Configure DRE_IS_ACCUMULATED em settings.py."
                            ),
                            "notes": "⚠ REVISÃO NECESSÁRIA: DRE_IS_ACCUMULATED não configurado.",
                        })
                        # _quarterly_diff: versão assumindo acumulado (para comparação)
                        records.append({
                            **base,
                            "metric_name": f"{flow_metric}_quarterly_diff",
                            "metric_value": diff_val,
                            "calculation_method": f"Versão assumindo DRE acumulada. {diff_note}",
                            "notes": "Hipótese: DRE acumulada. Compare com _quarterly para decidir.",
                        })

    if records:
        df_quarterly = pd.DataFrame(records)
        n_main = len(df_quarterly[df_quarterly["metric_name"].str.endswith("_quarterly")])
        n_diff = len(df_quarterly[df_quarterly["metric_name"].str.endswith("_quarterly_diff")])
        logger.info(
            f"Destrimestralização: {n_main} _quarterly + {n_diff} _quarterly_diff geradas "
            f"(modo={'acumulado' if mode is True else 'trimestral' if mode is False else 'desconhecido'})"
        )
        return pd.concat([df, df_quarterly], ignore_index=True)

    logger.info("Destrimestralização: nenhuma métrica de fluxo encontrada para processar")
    return df


def _make_quarterly_record(row, flow_metric: str, value: float, method: str, notes: str) -> dict:
    """Helper para criar registro de métrica quarterly a partir de uma row existente."""
    return {
        "bank_id": row["bank_id"],
        "bank_name": row["bank_name"],
        "ticker": row["ticker"],
        "reference_date": row["reference_date"],
        "year": int(row["year"]),
        "quarter": int(row["quarter"]),
        "metric_name": f"{flow_metric}_quarterly",
        "metric_value": value,
        "metric_unit": "R$ mil",
        "source_name": "Calculado",
        "source_table": "",
        "source_field": "",
        "calculation_method": method,
        "validation_status": "pendente",
        "institution_type_used": "",
        "notes": notes,
    }


# =============================================================================
# ORQUESTRADOR DE CÁLCULOS
# =============================================================================

def calculate_all_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula todas as métricas derivadas e as adiciona ao DataFrame.
    
    Ordem de execução:
    1. Destrimestralização de métricas de fluxo (LL, despesa PDD)
    2. ROE (usa lucro_liquido_quarterly se disponível, senão lucro_liquido)
    3. NPL, Cobertura, Custo do Risco
    
    Args:
        df: DataFrame normalizado com métricas extraídas diretamente
    
    Returns:
        DataFrame original + métricas calculadas concatenadas.
    """
    # Passo 0: destrimestralizar métricas de fluxo
    logger.info("Destrimestalizando métricas de fluxo (LL, despesa PDD)...")
    df = dequarterize_flow_metrics(df)

    calculated_dfs = []

    logger.info("Calculando ROE run rate trimestral...")
    calculated_dfs.append(calculate_roe_quarter_runrate(df))

    logger.info("Calculando ROE LTM...")
    calculated_dfs.append(calculate_roe_ltm(df))

    logger.info("Calculando NPL 90+...")
    calculated_dfs.append(calculate_npl_90(df))

    logger.info("Calculando cobertura...")
    calculated_dfs.append(calculate_coverage(df))

    logger.info("Calculando custo do risco...")
    calculated_dfs.append(calculate_cost_of_risk(df))

    # Concatenar tudo
    all_calculated = pd.concat(calculated_dfs, ignore_index=True)
    all_calculated = all_calculated[all_calculated["metric_name"].notna() & (all_calculated["metric_name"] != "")]

    logger.info(f"Total de métricas calculadas: {len(all_calculated)}")

    # Concatenar com o DataFrame original (que já inclui as _quarterly)
    result = pd.concat([df, all_calculated], ignore_index=True)
    return result
