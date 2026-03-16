"""
Orquestrador principal do pipeline de dados de crédito bancário.

Fluxo de execução:
1. Configuração e logging
2. Discovery de códigos de banco no IFData
3. Extração IFData (fonte primária)
4. Extração CVM (fonte secundária, bancos listados)
5. Normalização para schema padrão
6. Cálculo de métricas derivadas
7. Validação cruzada e sanity checks
8. Exportação da base final

Uso:
    python main.py

Configurações em config/settings.py
"""

import sys
import os
import time

# Adicionar diretório raiz ao path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from utils.logging_config import setup_logging
from config.banks import BANKS_MASTER, get_listed_banks
from config.settings import (
    get_ifdata_periods,
    IFDATA_REPORTS,
    METRIC_FIELD_MAP,
    DEFAULT_INSTITUTION_TYPE,
    FALLBACK_INSTITUTION_TYPE,
    OUTPUT_DIR,
)
from sources.ifdata_client import IFDataClient
from sources.cvm_client import CVMClient
from transforms.normalizer import (
    normalize_ifdata_report,
    normalize_cvm_data,
    consolidate_sources,
    create_empty_standard_df,
)
from transforms.calculator import calculate_all_derived_metrics
from transforms.validator import validate_all
from exports.exporter import export_all


def main():
    """Executa o pipeline completo."""
    start_time = time.time()

    # =========================================================================
    # ETAPA 0: SETUP
    # =========================================================================
    logger = setup_logging()
    logger.info("=" * 80)
    logger.info("PIPELINE DE DADOS DE CRÉDITO BANCÁRIO — INÍCIO")
    logger.info("=" * 80)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    periods = get_ifdata_periods()
    logger.info(f"Períodos a processar: {periods[0]} a {periods[-1]} ({len(periods)} trimestres)")
    logger.info(f"Bancos configurados: {list(BANKS_MASTER.keys())}")

    # =========================================================================
    # ETAPA 1: DISCOVERY DE CÓDIGOS NO IFDATA
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 1: Discovery de códigos no IFData")
    logger.info("-" * 60)

    ifdata = IFDataClient()

    # Usar um período confiável para discovery (não o último, que pode ser especulativo)
    # Se há 2+ períodos, usar o penúltimo; se só 1, usar esse mesmo.
    if len(periods) >= 2:
        discovery_period = periods[-2]
    else:
        discovery_period = periods[-1]
    logger.info(f"Período usado para discovery de bancos: {discovery_period}")

    bank_codes: dict[str, int] = {}
    bank_types: dict[str, int] = {}
    discovery_failures: list[str] = []

    for bank_id, config in BANKS_MASTER.items():
        code = ifdata.discover_bank_code(
            config.ifdata_name_pattern,
            discovery_period,
            config.institution_type
        )
        if code is not None:
            bank_codes[bank_id] = code
            # Registrar o tipo preferencial; se o discovery fez fallback para
            # individual (tipo 3), a função retorna código mas não informa o tipo.
            # Precisamos verificar se o banco existe no tipo preferencial.
            # Se não, assumir que foi fallback.
            df_check = ifdata.get_cadastro(discovery_period, config.institution_type)
            if not df_check.empty:
                name_col = ifdata._find_name_column(df_check)
                if name_col:
                    found_in_preferred = df_check[name_col].str.upper().str.contains(
                        config.ifdata_name_pattern.upper(), na=False
                    ).any()
                    if found_in_preferred:
                        bank_types[bank_id] = config.institution_type
                    else:
                        bank_types[bank_id] = FALLBACK_INSTITUTION_TYPE
                        logger.info(f"  {bank_id}: encontrado via fallback (tipo {FALLBACK_INSTITUTION_TYPE})")
                else:
                    bank_types[bank_id] = config.institution_type
            else:
                bank_types[bank_id] = config.institution_type
            logger.info(f"  ✓ {bank_id}: CodInst={code} (tipo={bank_types[bank_id]})")
        else:
            discovery_failures.append(bank_id)
            logger.warning(f"  ✗ {bank_id}: NÃO ENCONTRADO")

    logger.info(
        f"Discovery concluído: {len(bank_codes)} encontrados, "
        f"{len(discovery_failures)} falharam"
    )
    if discovery_failures:
        logger.warning(f"Bancos não encontrados: {discovery_failures}")

    # =========================================================================
    # ETAPA 2: DISCOVERY DE RELATÓRIOS DISPONÍVEIS
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 2: Discovery de relatórios disponíveis")
    logger.info("-" * 60)

    report_list = ifdata.get_report_list()
    if not report_list.empty:
        logger.info(f"Relatórios disponíveis: {len(report_list)} entradas")
        logger.debug(f"Colunas da lista de relatórios: {report_list.columns.tolist()}")
        # Salvar para referência
        report_list.to_csv(
            os.path.join(OUTPUT_DIR, "ifdata_report_structure.csv"),
            index=False, encoding="utf-8-sig"
        )
    else:
        logger.warning("Não foi possível obter lista de relatórios. Usando códigos padrão.")

    # =========================================================================
    # ETAPA 3: EXTRAÇÃO IFDATA
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 3: Extração de dados do IFData")
    logger.info("-" * 60)

    all_ifdata_normalized = []
    checkpoint_file = os.path.join(OUTPUT_DIR, "_checkpoint_ifdata.csv")

    # Tentar retomar de checkpoint
    completed_periods = set()
    if os.path.exists(checkpoint_file):
        try:
            df_checkpoint = pd.read_csv(checkpoint_file, encoding="utf-8-sig")
            all_ifdata_normalized.append(df_checkpoint)
            # Identificar períodos já processados
            if "reference_date" in df_checkpoint.columns and "year" in df_checkpoint.columns:
                for _, row in df_checkpoint[["year", "quarter"]].drop_duplicates().iterrows():
                    y, q = int(row["year"]), int(row["quarter"])
                    # Reconstruir o período YYYYMM
                    month_map = {1: 3, 2: 6, 3: 9, 4: 12}
                    p = y * 100 + month_map.get(q, q * 3)
                    completed_periods.add(p)
            logger.info(
                f"Checkpoint carregado: {len(df_checkpoint)} registros, "
                f"{len(completed_periods)} períodos já processados"
            )
        except Exception as e:
            logger.warning(f"Checkpoint corrompido, ignorando: {e}")
            completed_periods = set()

    periods_to_process = [p for p in periods if p not in completed_periods]
    if completed_periods:
        logger.info(f"Períodos restantes: {len(periods_to_process)} de {len(periods)}")

    new_records_since_checkpoint = []

    for i, periodo in enumerate(periods_to_process):
        logger.info(f"Processando período {periodo} ({i+1}/{len(periods_to_process)})...")

        for report_key, report_code in IFDATA_REPORTS.items():
            # Quais métricas esperamos neste relatório?
            metrics_in_report = {
                k: v for k, v in METRIC_FIELD_MAP.items()
                if v.get("report") == report_key
            }

            if not metrics_in_report:
                continue

            # Extrair relatório para todos os bancos
            df_report = ifdata.extract_all_banks_period(
                bank_codes, periodo, bank_types, report_code
            )

            if df_report.empty:
                logger.debug(f"Sem dados para relatório={report_key}, período={periodo}")
                continue

            # Normalizar para cada banco
            for bank_id in bank_codes:
                mask = df_report.get("bank_id") == bank_id
                if mask is None or not hasattr(mask, "any"):
                    continue
                if not mask.any():
                    continue

                df_bank = df_report[mask]
                df_norm = normalize_ifdata_report(
                    df_bank, bank_id, periodo, report_key, metrics_in_report,
                    institution_type_used=bank_types.get(bank_id)
                )

                if not df_norm.empty:
                    all_ifdata_normalized.append(df_norm)
                    new_records_since_checkpoint.append(df_norm)

        # Salvar checkpoint a cada 5 períodos processados
        if (i + 1) % 5 == 0 and new_records_since_checkpoint:
            try:
                df_save = pd.concat(all_ifdata_normalized, ignore_index=True)
                df_save.to_csv(checkpoint_file, index=False, encoding="utf-8-sig")
                logger.info(f"Checkpoint salvo: {len(df_save)} registros ({i+1} períodos processados)")
            except Exception as e:
                logger.warning(f"Falha ao salvar checkpoint: {e}")

    if all_ifdata_normalized:
        df_ifdata = pd.concat(all_ifdata_normalized, ignore_index=True)
        logger.info(f"IFData: {len(df_ifdata)} registros normalizados")
        # Salvar checkpoint final
        try:
            df_ifdata.to_csv(checkpoint_file, index=False, encoding="utf-8-sig")
        except Exception:
            pass
    else:
        df_ifdata = create_empty_standard_df()
        logger.warning("IFData: NENHUM dado extraído!")

    # =========================================================================
    # ETAPA 4: EXTRAÇÃO CVM (bancos listados)
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 4: Extração de dados da CVM")
    logger.info("-" * 60)

    all_cvm_normalized = []
    cvm_client = CVMClient()
    listed_banks = get_listed_banks()

    for bank_id, config in listed_banks.items():
        if not config.cvm_name_pattern:
            logger.debug(f"Sem padrão CVM para {bank_id}, pulando")
            continue

        logger.info(f"Extraindo CVM: {bank_id} ({config.cvm_name_pattern})")

        df_cvm_raw = cvm_client.extract_bank_metrics(
            cnpj_root=config.cnpj_root,
            cvm_name_pattern=config.cvm_name_pattern
        )

        if not df_cvm_raw.empty:
            df_cvm_norm = normalize_cvm_data(df_cvm_raw, bank_id)
            if not df_cvm_norm.empty:
                all_cvm_normalized.append(df_cvm_norm)

    if all_cvm_normalized:
        df_cvm = pd.concat(all_cvm_normalized, ignore_index=True)
        logger.info(f"CVM: {len(df_cvm)} registros normalizados")
    else:
        df_cvm = create_empty_standard_df()
        logger.info("CVM: nenhum dado extraído (esperado se rede está indisponível)")

    # =========================================================================
    # ETAPA 5: CONSOLIDAÇÃO
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 5: Consolidação de fontes")
    logger.info("-" * 60)

    df_consolidated = consolidate_sources(df_ifdata, df_cvm, prefer_source="IFData")
    logger.info(f"Base consolidada: {len(df_consolidated)} registros")

    # =========================================================================
    # ETAPA 6: CÁLCULO DE MÉTRICAS DERIVADAS
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 6: Cálculo de métricas derivadas")
    logger.info("-" * 60)

    df_with_calcs = calculate_all_derived_metrics(df_consolidated)
    logger.info(f"Base com cálculos: {len(df_with_calcs)} registros")

    # =========================================================================
    # ETAPA 7: VALIDAÇÃO
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 7: Validação e sanity checks")
    logger.info("-" * 60)

    df_validated = validate_all(df_with_calcs)

    # =========================================================================
    # ETAPA 8: EXPORTAÇÃO
    # =========================================================================
    logger.info("-" * 60)
    logger.info("ETAPA 8: Exportação")
    logger.info("-" * 60)

    output_paths = export_all(df_validated)

    # =========================================================================
    # RELATÓRIO FINAL
    # =========================================================================
    elapsed = time.time() - start_time
    logger.info("=" * 80)
    logger.info("PIPELINE CONCLUÍDO")
    logger.info("=" * 80)
    logger.info(f"Tempo total: {elapsed:.1f} segundos")
    logger.info(f"Registros na base final: {len(df_validated)}")
    logger.info(f"Bancos processados: {df_validated['bank_id'].nunique() if not df_validated.empty else 0}")
    logger.info(f"Métricas disponíveis: {df_validated['metric_name'].nunique() if not df_validated.empty else 0}")

    if not df_validated.empty:
        logger.info(f"\nResumo por banco:")
        for bank_id in sorted(df_validated["bank_id"].unique()):
            count = len(df_validated[df_validated["bank_id"] == bank_id])
            metrics = df_validated[df_validated["bank_id"] == bank_id]["metric_name"].nunique()
            quarters = df_validated[df_validated["bank_id"] == bank_id]["quarter"].nunique()
            logger.info(f"  {bank_id}: {count} registros, {metrics} métricas, {quarters} trimestres")

        logger.info(f"\nResumo por métrica:")
        for metric in sorted(df_validated["metric_name"].unique()):
            count = len(df_validated[df_validated["metric_name"] == metric])
            banks = df_validated[df_validated["metric_name"] == metric]["bank_id"].nunique()
            logger.info(f"  {metric}: {count} registros ({banks} bancos)")

    logger.info(f"\nArquivos gerados:")
    for key, path in output_paths.items():
        logger.info(f"  {key}: {path}")

    # Limpar checkpoints após execução bem-sucedida
    for ckpt in ["_checkpoint_ifdata.csv"]:
        ckpt_path = os.path.join(OUTPUT_DIR, ckpt)
        if os.path.exists(ckpt_path):
            os.remove(ckpt_path)
            logger.debug(f"Checkpoint removido: {ckpt_path}")

    logger.info("=" * 80)

    return df_validated


if __name__ == "__main__":
    main()
