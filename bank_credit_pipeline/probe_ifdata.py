"""
Script de sondagem (probe) da API IFData.

Objetivo: fazer chamadas exploratórias à API OData do IFData para descobrir
a estrutura real dos dados antes de rodar o pipeline completo.

O que este script faz:
1. Lista todos os relatórios disponíveis
2. Busca o cadastro de instituições para descobrir nomes e códigos reais
3. Para cada relatório configurado, faz uma chamada para 1 banco e 1 período
4. Salva amostras brutas e um resumo CSV
5. Gera um relatório de compatibilidade entre os search_patterns configurados
   e a estrutura real da API

Uso:
    python probe_ifdata.py

Saída em output/probe/:
    - probe_report_list.csv
    - probe_cadastro.csv
    - probe_relatorio_X_raw.json
    - probe_column_map.csv
    - probe_summary.txt
"""

import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    IFDATA_REPORTS,
    METRIC_FIELD_MAP,
    DEFAULT_INSTITUTION_TYPE,
    OUTPUT_DIR,
    get_ifdata_periods,
)
from config.banks import BANKS_MASTER
from sources.ifdata_client import IFDataClient
from utils.logging_config import setup_logging

PROBE_DIR = os.path.join(OUTPUT_DIR, "probe")


def normalize_text(value) -> str:
    """Normaliza texto para matching robusto."""
    if value is None:
        return ""

    value = str(value).strip().upper()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def find_metric_rows_long(df: pd.DataFrame, patterns: list[str]) -> pd.DataFrame:
    """
    Busca métricas no formato longo do IFData.

    O IFData costuma vir com colunas como:
    Conta, NomeColuna, DescricaoColuna, Saldo

    Então a métrica pode estar em uma linha, e não no nome da coluna.
    """
    if df.empty:
        return pd.DataFrame()

    text_cols = [
        col for col in ["Conta", "Grupo", "NomeColuna", "DescricaoColuna", "NomeRelatorio"]
        if col in df.columns
    ]

    if not text_cols:
        return pd.DataFrame()

    normalized_patterns = [normalize_text(p) for p in patterns if p]

    combined = pd.Series("", index=df.index)
    for col in text_cols:
        combined = combined + " " + df[col].fillna("").astype(str).map(normalize_text)

    mask = pd.Series(False, index=df.index)
    for pattern in normalized_patterns:
        mask = mask | combined.str.contains(re.escape(pattern), na=False)

    return df[mask].copy()


def try_parse_float(value):
    """Converte valor para float de forma defensiva."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        value = str(value).replace(" ", "").replace(",", ".")
        return float(value)
    except (ValueError, TypeError):
        return None


def probe():
    logger = setup_logging()
    logger.info("=" * 70)
    logger.info("SONDAGEM DA API IFDATA - INÍCIO")
    logger.info("=" * 70)

    os.makedirs(PROBE_DIR, exist_ok=True)
    client = IFDataClient()

    periods = get_ifdata_periods()
    if len(periods) >= 4:
        probe_period = periods[-4]
    else:
        probe_period = periods[0]

    logger.info(f"Período de sondagem: {probe_period}")

    probe_bank_config = BANKS_MASTER["ITAU"]
    summary_lines = []

    # =========================================================================
    # 1. LISTA DE RELATÓRIOS
    # =========================================================================
    logger.info("-" * 50)
    logger.info("1. Listando relatórios disponíveis...")
    report_list = client.get_report_list()

    report_lookup = {}
    if not report_list.empty:
        report_list.to_csv(
            os.path.join(PROBE_DIR, "probe_report_list.csv"),
            index=False,
            encoding="utf-8-sig",
        )
        logger.info(f"   {len(report_list)} entradas na lista de relatórios")
        logger.info(f"   Colunas: {report_list.columns.tolist()}")

        summary_lines.append(f"LISTA DE RELATÓRIOS: {len(report_list)} entradas")
        summary_lines.append(f"  Colunas: {report_list.columns.tolist()}")

        for _, row in report_list.head(20).iterrows():
            summary_lines.append(f"  {dict(row)}")

        if "NomeRelatorio" in report_list.columns and "NumeroRelatorio" in report_list.columns:
            report_lookup = {
                normalize_text(nome): str(numero)
                for nome, numero in zip(report_list["NomeRelatorio"], report_list["NumeroRelatorio"])
            }

        # Check simples para inconsistências conhecidas da configuração
        capital_oficial = report_lookup.get(normalize_text("Informações de Capital"))
        capital_config = str(IFDATA_REPORTS.get("capital", ""))

        if capital_oficial and capital_config and capital_oficial != capital_config:
            summary_lines.append("")
            summary_lines.append("ALERTA DE CONFIGURAÇÃO:")
            summary_lines.append(
                f"  IFDATA_REPORTS['capital'] está como {capital_config}, "
                f"mas a lista oficial indica {capital_oficial}."
            )
            logger.warning(
                f"IFDATA_REPORTS['capital']={capital_config}, mas o relatório oficial "
                f"'Informações de Capital' é {capital_oficial}."
            )

        credito_risco_oficial = report_lookup.get(
            normalize_text("Carteira de crédito por nível de risco da operação")
        )
        credito_config = str(IFDATA_REPORTS.get("credito", ""))

        if credito_risco_oficial and credito_config and credito_risco_oficial != credito_config:
            summary_lines.append("")
            summary_lines.append("ALERTA DE CONFIGURAÇÃO:")
            summary_lines.append(
                f"  IFDATA_REPORTS['credito'] está como {credito_config}, "
                f"mas o relatório de risco oficial indica {credito_risco_oficial}."
            )
            logger.warning(
                f"IFDATA_REPORTS['credito']={credito_config}, mas o relatório oficial "
                f"'Carteira de crédito por nível de risco da operação' é {credito_risco_oficial}."
            )

        credito_indexador_oficial = report_lookup.get(
            normalize_text("Carteira de crédito por indexador / composição")
        )
        credito_indexador_config = str(IFDATA_REPORTS.get("credito_indexador", ""))

        if (
            credito_indexador_oficial
            and credito_indexador_config
            and credito_indexador_oficial != credito_indexador_config
        ):
            summary_lines.append("")
            summary_lines.append("ALERTA DE CONFIGURAÇÃO:")
            summary_lines.append(
                "  IFDATA_REPORTS['credito_indexador'] diverge do código oficial "
                f"({credito_indexador_config} vs {credito_indexador_oficial})."
            )
            logger.warning(
                "IFDATA_REPORTS['credito_indexador']=%s, mas o relatório oficial "
                "'Carteira de crédito por indexador / composição' é %s.",
                credito_indexador_config,
                credito_indexador_oficial,
            )

        summary_lines.append("")
    else:
        logger.warning("   FALHA ao obter lista de relatórios!")
        summary_lines.append("LISTA DE RELATÓRIOS: FALHA NA OBTENÇÃO")
        summary_lines.append("")

    # =========================================================================
    # 2. CADASTRO DE INSTITUIÇÕES
    # =========================================================================
    logger.info("-" * 50)
    logger.info("2. Sondando cadastro de instituições...")

    df_cad = client.get_cadastro(probe_period)

    if not df_cad.empty:
        cadastro_path = os.path.join(PROBE_DIR, "probe_cadastro.csv")
        df_cad.to_csv(cadastro_path, index=False, encoding="utf-8-sig")

        logger.info(f"   {len(df_cad)} instituições encontradas")
        logger.info(f"   Colunas: {df_cad.columns.tolist()}")

        summary_lines.append(f"CADASTRO: {len(df_cad)} instituições")
        summary_lines.append(f"  Colunas: {df_cad.columns.tolist()}")

        name_col = client._find_name_column(df_cad)
        code_col = client._find_code_column(df_cad)

        if name_col and code_col:
            summary_lines.append("")
            summary_lines.append("BUSCA POR BANCOS CONFIGURADOS:")
            for bank_id, bank_cfg in BANKS_MASTER.items():
                code = client.discover_bank_code(
                    name_pattern=bank_cfg.ifdata_name_pattern,
                    name_alternatives=getattr(bank_cfg, "ifdata_name_alternatives", []),
                    periodo=probe_period,
                    tipo_instituicao=bank_cfg.institution_type,
                )

                if code is not None:
                    row = df_cad[df_cad[code_col].astype(str) == str(code)].head(1)
                    if not row.empty:
                        bank_name = row.iloc[0][name_col]
                        summary_lines.append(
                            f"  {bank_id}: ENCONTRADO -> CodInst={code}, nome='{bank_name}'"
                        )
                    else:
                        summary_lines.append(
                            f"  {bank_id}: ENCONTRADO -> CodInst={code}, mas linha não localizada no cadastro"
                        )
                else:
                    summary_lines.append(
                        f"  {bank_id}: NÃO ENCONTRADO "
                        f"(pattern='{bank_cfg.ifdata_name_pattern}', "
                        f"aliases={getattr(bank_cfg, 'ifdata_name_alternatives', [])})"
                    )

        summary_lines.append("")
    else:
        logger.warning("   FALHA ao obter cadastro")
        summary_lines.append("CADASTRO: FALHA")
        summary_lines.append("")

    # =========================================================================
    # 3. SONDAGEM DE CADA RELATÓRIO
    # =========================================================================
    logger.info("-" * 50)
    logger.info("3. Sondando relatórios de dados...")

    itau_code = client.discover_bank_code(
        name_pattern=probe_bank_config.ifdata_name_pattern,
        name_alternatives=getattr(probe_bank_config, "ifdata_name_alternatives", []),
        periodo=probe_period,
        tipo_instituicao=probe_bank_config.institution_type,
    )

    column_map_rows = []

    for report_key, report_code in IFDATA_REPORTS.items():
        logger.info(f"   Relatório '{report_key}' (código={report_code})...")

        preferred_type = probe_bank_config.institution_type
        fallback_type = 3 if preferred_type == 2 else 2
        used_type = preferred_type

        df_report = client.get_report_data(
            probe_period,
            preferred_type,
            str(report_code),
        )

        if df_report.empty:
            df_report = client.get_report_data(
                probe_period,
                fallback_type,
                str(report_code),
            )
            used_type = fallback_type

        summary_lines.append(f"RELATÓRIO: {report_key} (código={report_code})")

        if df_report.empty:
            logger.warning("   VAZIO ou FALHA")
            summary_lines.append("  RESULTADO: VAZIO / FALHA")
            summary_lines.append("")
            continue

        logger.info(
            f"   {len(df_report)} linhas, {len(df_report.columns)} colunas, tipo usado={used_type}"
        )
        summary_lines.append(
            f"  Linhas: {len(df_report)}, Colunas: {len(df_report.columns)}, tipo usado: {used_type}"
        )
        summary_lines.append("  Nomes das colunas:")
        for i, col in enumerate(df_report.columns):
            summary_lines.append(f"    [{i}] {col}")

        sample = df_report.head(10).to_dict(orient="records")
        raw_path = os.path.join(PROBE_DIR, f"probe_relatorio_{report_key}_raw.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=2, default=str)

        code_col = client._find_code_column(df_report)

        if itau_code is not None and code_col is not None:
            itau_data = df_report[df_report[code_col].astype(str) == str(itau_code)]
            if not itau_data.empty:
                summary_lines.append(f"\n  Amostra do ITAU (CodInst={itau_code}):")
                for _, row in itau_data.head(5).iterrows():
                    summary_lines.append(f"    {row.to_dict()}")
            else:
                summary_lines.append(
                    f"\n  ITAU (CodInst={itau_code}) não encontrado neste relatório/tipo"
                )

        metrics_in_report = {
            k: v for k, v in METRIC_FIELD_MAP.items()
            if v.get("report") == report_key
        }

        summary_lines.append("\n  MAPEAMENTO DE MÉTRICAS:")
        for metric_name, metric_cfg in metrics_in_report.items():
            patterns = metric_cfg.get("search_patterns", [])

            matched_col = None
            matched_pattern = None
            matched_mode = ""
            example_text = ""

            # 1. Tenta match por nome de coluna
            for col in df_report.columns:
                col_norm = normalize_text(col)
                for pattern in patterns:
                    if normalize_text(pattern) in col_norm:
                        matched_col = col
                        matched_pattern = pattern
                        matched_mode = "COLUNA"
                        break
                if matched_col:
                    break

            # 2. Se não achou por coluna, tenta no formato longo por linhas
            if not matched_col:
                metric_rows = find_metric_rows_long(df_report, patterns)
                if not metric_rows.empty:
                    matched_mode = "LINHA"
                    matched_pattern = patterns[0] if patterns else ""
                    example_row = metric_rows.iloc[0].to_dict()
                    example_text = str(example_row)

            status = "MATCH" if (matched_col or matched_mode == "LINHA") else "SEM_MATCH"

            if status == "MATCH" and matched_mode == "COLUNA":
                summary_lines.append(
                    f"    {metric_name}: MATCH_COLUNA -> coluna='{matched_col}' "
                    f"(pattern='{matched_pattern}')"
                )
            elif status == "MATCH" and matched_mode == "LINHA":
                summary_lines.append(
                    f"    {metric_name}: MATCH_LINHA -> pattern='{matched_pattern}' "
                    f"encontrado na estrutura longa"
                )
                summary_lines.append(f"      Exemplo: {example_text}")
            else:
                summary_lines.append(f"    {metric_name}: SEM_MATCH")
                summary_lines.append(f"      Patterns tentados: {patterns}")

            column_map_rows.append(
                {
                    "report_key": report_key,
                    "report_code": report_code,
                    "metric_name": metric_name,
                    "configured_patterns": "|".join(patterns),
                    "matched_column": matched_col or "",
                    "matched_pattern": matched_pattern or "",
                    "matched_mode": matched_mode,
                    "status": status,
                    "all_columns": "|".join(df_report.columns.tolist()),
                    "example_row_if_long": example_text,
                }
            )

        summary_lines.append("")

    # =========================================================================
    # 4. SALVAR MAPA DE COLUNAS
    # =========================================================================
    if column_map_rows:
        map_path = os.path.join(PROBE_DIR, "probe_column_map.csv")
        with open(map_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=column_map_rows[0].keys())
            writer.writeheader()
            writer.writerows(column_map_rows)
        logger.info(f"Mapa de colunas salvo: {map_path}")

    # =========================================================================
    # 4b. ANÁLISE DE DRE: ACUMULADA OU TRIMESTRAL?
    # =========================================================================
    logger.info("-" * 50)
    logger.info("4b. Analisando se DRE é acumulada ou trimestral...")

    summary_lines.append("")
    summary_lines.append("=" * 50)
    summary_lines.append("ANÁLISE DRE: ACUMULADA OU TRIMESTRAL?")
    summary_lines.append("=" * 50)

    all_periods = get_ifdata_periods()
    year_periods = {}
    for p in all_periods:
        yr = p // 100
        year_periods.setdefault(yr, []).append(p)

    analysis_year = None
    for yr in sorted(year_periods.keys(), reverse=True):
        if len(year_periods[yr]) == 4:
            analysis_year = yr
            break

    if analysis_year and itau_code:
        summary_lines.append(f"Ano analisado: {analysis_year}")
        summary_lines.append(f"Banco: Itaú (CodInst={itau_code})")

        ll_by_quarter = {}
        resumo_code = str(IFDATA_REPORTS.get("resumo", "1"))

        for periodo in sorted(year_periods[analysis_year]):
            preferred_type = probe_bank_config.institution_type
            fallback_type = 3 if preferred_type == 2 else 2

            df_rep = client.get_report_data(periodo, preferred_type, resumo_code)
            used_type = preferred_type

            if df_rep.empty:
                df_rep = client.get_report_data(periodo, fallback_type, resumo_code)
                used_type = fallback_type

            if df_rep.empty:
                continue

            code_col = client._find_code_column(df_rep)
            if not code_col:
                continue

            itau_rows = df_rep[df_rep[code_col].astype(str) == str(itau_code)]
            if itau_rows.empty:
                continue

            lucro_rows = find_metric_rows_long(
                itau_rows,
                ["LUCRO LIQUIDO", "RESULTADO LIQUIDO"],
            )

            if lucro_rows.empty or "Saldo" not in lucro_rows.columns:
                continue

            val = try_parse_float(lucro_rows.iloc[0]["Saldo"])
            if val is None:
                continue

            month = periodo % 100
            quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
            if quarter:
                ll_by_quarter[quarter] = val
                logger.info(
                    f"   {periodo}: lucro líquido encontrado para Itaú, "
                    f"tipo={used_type}, valor={val:,.0f}"
                )

        if len(ll_by_quarter) >= 3:
            sorted_qs = sorted(ll_by_quarter.keys())
            vals = [ll_by_quarter[q] for q in sorted_qs]

            summary_lines.append("\nLucro Líquido do Itaú:")
            for q in sorted_qs:
                summary_lines.append(f"  Q{q}: {ll_by_quarter[q]:,.0f}")

            ratio = abs(vals[-1]) / abs(vals[0]) if vals[0] != 0 else 0
            summary_lines.append(f"\nRatio Q{sorted_qs[-1]}/Q{sorted_qs[0]}: {ratio:.2f}x")

            if ratio >= 2.5:
                summary_lines.append("\nRECOMENDAÇÃO: DRE_IS_ACCUMULATED = True")
                summary_lines.append("  Razão: padrão sugere valor acumulado no ano.")
            elif ratio <= 1.8:
                summary_lines.append("\nRECOMENDAÇÃO: DRE_IS_ACCUMULATED = False")
                summary_lines.append("  Razão: padrão sugere valor já trimestral.")
            else:
                summary_lines.append("\nRECOMENDAÇÃO: INCONCLUSIVO")
                summary_lines.append("  Razão: ratio ficou em faixa intermediária.")
        else:
            summary_lines.append(
                f"Dados insuficientes: apenas {len(ll_by_quarter)} trimestres encontrados"
            )
    else:
        if not analysis_year:
            summary_lines.append("Nenhum ano completo disponível para análise.")
        if not itau_code:
            summary_lines.append("Itaú não encontrado no cadastro.")
        logger.warning("   Não foi possível analisar DRE")

    summary_lines.append("")

    # =========================================================================
    # 5. SALVAR RESUMO
    # =========================================================================
    summary_path = os.path.join(PROBE_DIR, "probe_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("SONDAGEM DA API IFDATA - RELATÓRIO\n")
        f.write(f"Período: {probe_period}\n")
        f.write(f"Data: {datetime.now().isoformat()}\n")
        f.write("=" * 70 + "\n\n")
        f.write("\n".join(summary_lines))

    logger.info(f"Resumo salvo: {summary_path}")

    # =========================================================================
    # 6. RESUMO NO CONSOLE
    # =========================================================================
    logger.info("=" * 70)
    logger.info("RESUMO DA SONDAGEM")
    logger.info("=" * 70)

    matches = sum(1 for r in column_map_rows if r["status"] == "MATCH")
    misses = sum(1 for r in column_map_rows if r["status"] == "SEM_MATCH")

    logger.info(f"Métricas com match: {matches}")
    logger.info(f"Métricas sem match: {misses}")

    if misses > 0:
        logger.warning(
            "AÇÃO NECESSÁRIA: revise probe_column_map.csv e ajuste METRIC_FIELD_MAP em settings.py"
        )

    logger.info(f"Arquivos gerados em {PROBE_DIR}/")
    logger.info("=" * 70)


if __name__ == "__main__":
    probe()
