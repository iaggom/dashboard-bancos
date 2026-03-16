"""
Cliente para dados abertos da CVM (Comissão de Valores Mobiliários).

Responsabilidades:
1. Baixar arquivos CSV de ITR (trimestrais) e DFP (anuais)
2. Filtrar por bancos listados
3. Extrair Lucro Líquido, Patrimônio Líquido e outras métricas contábeis
4. Servir como fonte de validação cruzada com dados do IFData

Estrutura dos dados CVM:
- URL: https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_YYYY.zip
- URL: https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_YYYY.zip
- Cada zip contém múltiplos CSVs:
  - *_BPA_con_*.csv (Balanço Patrimonial Ativo - Consolidado)
  - *_BPP_con_*.csv (Balanço Patrimonial Passivo - Consolidado)
  - *_DRE_con_*.csv (DRE - Consolidado)
  - *_ind_*.csv (Individual)
- Prefere-se dados consolidados (_con_) para bancos com conglomerado

Colunas típicas dos CSVs da CVM:
- CNPJ_CIA: CNPJ da companhia
- DENOM_CIA: Nome da companhia
- DT_REFER: Data de referência (YYYY-MM-DD)
- DT_FIM_EXERC: Data final do exercício
- VERSAO: Versão do documento (pega-se a mais recente)
- CD_CONTA: Código da conta contábil
- DS_CONTA: Descrição da conta
- VL_CONTA: Valor da conta
- ST_CONTA_FIXA: S = conta fixa do plano
- ESCALA_MOEDA: UNIDADE ou MIL
- MOEDA_REF: BRL
"""

import io
import logging
import os
import zipfile
from typing import Optional

import pandas as pd
import requests

from config.settings import CVM_BASE_URL, CVM_TIMEOUT, CACHE_DIR, START_YEAR, END_YEAR

logger = logging.getLogger("bank_credit_pipeline.sources.cvm")


# =============================================================================
# CONTAS CONTÁBEIS DE INTERESSE PARA BANCOS
# =============================================================================
# Bancos usam plano de contas COSIF, mas na CVM reportam usando contas padronizadas.
# Atenção: instituições financeiras usam BP e DRE com estrutura diferente de não-financeiras.
#
# As contas abaixo são aproximações baseadas no padrão CVM para IFs.
# O pipeline busca por CD_CONTA (código) e DS_CONTA (descrição) simultaneamente
# para maior robustez, já que os códigos podem variar entre instituições.
# =============================================================================

CVM_ACCOUNT_MAP = {
    "patrimonio_liquido": {
        "cd_conta_patterns": ["2.03", "2.07"],  # PL em bancos pode ter código diferente
        "ds_conta_patterns": [
            "Patrimônio Líquido",
            "Patrimônio Líquido Consolidado",
            "PATRIMÔNIO LÍQUIDO"
        ],
        "statement": "BPP",  # Balanço Patrimonial - Passivo
        "description": "Patrimônio Líquido (BP)"
    },
    "lucro_liquido": {
        "cd_conta_patterns": ["3.11", "3.99"],  # Última linha da DRE
        "ds_conta_patterns": [
            "Lucro/Prejuízo Consolidado do Período",
            "Lucro/Prejuízo do Período",
            "Lucro Líquido",
            "Resultado Líquido"
        ],
        "statement": "DRE",
        "description": "Lucro Líquido (DRE)"
    },
    "ativo_total": {
        "cd_conta_patterns": ["1"],  # Conta raiz do ativo — MATCH EXATO
        "ds_conta_patterns": ["Ativo Total", "ATIVO TOTAL"],
        "statement": "BPA",  # Balanço Patrimonial - Ativo
        "description": "Ativo Total (BP)",
        "match_mode": "exact"  # Sinaliza que CD_CONTA deve ser match exato, não startswith
    },
}


class CVMClient:
    """
    Cliente para dados abertos da CVM.
    
    Baixa e processa ITRs e DFPs para bancos listados em bolsa.
    """

    def __init__(self):
        self.base_url = CVM_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BankCreditPipeline/1.0"
        })
        os.makedirs(CACHE_DIR, exist_ok=True)

    # =========================================================================
    # DOWNLOAD E CACHE
    # =========================================================================

    def _download_zip(self, doc_type: str, year: int) -> Optional[bytes]:
        """
        Baixa arquivo ZIP da CVM.
        
        Cache:
        - Anos anteriores ao corrente: cache permanente (dados consolidados, não mudam)
        - Ano corrente: cache de 7 dias (pode ter reapresentações ao longo do ano)
        
        Args:
            doc_type: "ITR" ou "DFP"
            year: Ano (ex: 2023)
        
        Returns:
            Conteúdo do ZIP em bytes, ou None se falhar.
        """
        import time as _time
        from datetime import date as _date

        cache_file = os.path.join(CACHE_DIR, f"{doc_type.lower()}_cia_aberta_{year}.zip")

        # Verificar cache local
        if os.path.exists(cache_file):
            # Ano corrente: invalidar cache após 7 dias
            if year >= _date.today().year:
                file_age_days = (_time.time() - os.path.getmtime(cache_file)) / 86400
                if file_age_days > 7:
                    logger.info(f"Cache expirado ({file_age_days:.0f} dias, ano corrente): {cache_file}")
                    os.remove(cache_file)
                else:
                    logger.debug(f"Cache hit (ano corrente, {file_age_days:.0f}d): {cache_file}")
                    with open(cache_file, "rb") as f:
                        return f.read()
            else:
                # Anos anteriores: cache permanente
                logger.debug(f"Cache hit (permanente): {cache_file}")
                with open(cache_file, "rb") as f:
                    return f.read()

        url = f"{self.base_url}/{doc_type}/DADOS/{doc_type.lower()}_cia_aberta_{year}.zip"
        logger.info(f"Baixando: {url}")

        try:
            resp = self.session.get(url, timeout=CVM_TIMEOUT)
            if resp.status_code == 200:
                # Salvar cache
                with open(cache_file, "wb") as f:
                    f.write(resp.content)
                logger.info(f"Download concluído: {len(resp.content)} bytes")
                return resp.content
            elif resp.status_code == 404:
                logger.warning(f"Arquivo não encontrado (404): {url}")
                return None
            else:
                logger.error(f"HTTP {resp.status_code}: {url}")
                return None
        except requests.exceptions.Timeout:
            logger.error(f"Timeout ao baixar: {url}")
            return None
        except Exception as e:
            logger.error(f"Erro ao baixar {url}: {type(e).__name__}: {e}")
            return None

    def _extract_csv_from_zip(
        self,
        zip_content: bytes,
        statement: str,
        consolidated: bool = True
    ) -> pd.DataFrame:
        """
        Extrai CSV específico de dentro do ZIP.
        
        Args:
            zip_content: Conteúdo do ZIP em bytes
            statement: Tipo de demonstração ("BPA", "BPP", "DRE")
            consolidated: Se True, busca "_con_"; se False, "_ind_"
        
        Returns:
            DataFrame do CSV encontrado. Vazio se não encontrar.
        """
        suffix = "_con_" if consolidated else "_ind_"
        search = f"_{statement.lower()}{suffix}"

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                matching_files = [
                    name for name in zf.namelist()
                    if search in name.lower() and name.endswith(".csv")
                ]

                if not matching_files:
                    # Tentar sem _con_/_ind_ (alguns arquivos podem não ter)
                    search_alt = f"_{statement.lower()}_"
                    matching_files = [
                        name for name in zf.namelist()
                        if search_alt in name.lower() and name.endswith(".csv")
                    ]

                if not matching_files:
                    logger.warning(
                        f"CSV não encontrado para statement={statement}, "
                        f"consolidated={consolidated}. Arquivos no ZIP: {zf.namelist()}"
                    )
                    return pd.DataFrame()

                # Pegar o primeiro match
                csv_file = matching_files[0]
                logger.debug(f"Extraindo: {csv_file}")

                with zf.open(csv_file) as f:
                    # CVM usa encoding latin-1 ou UTF-8 com BOM
                    raw_bytes = f.read()

                # Tentar latin-1 primeiro (mais comum nos CSVs da CVM)
                try:
                    df = pd.read_csv(
                        io.BytesIO(raw_bytes), sep=";", encoding="latin-1", dtype=str
                    )
                except Exception:
                    try:
                        df = pd.read_csv(
                            io.BytesIO(raw_bytes), sep=";", encoding="utf-8-sig", dtype=str
                        )
                    except Exception as enc_err:
                        logger.error(f"Falha ao decodificar CSV {csv_file}: {enc_err}")
                        return pd.DataFrame()

                logger.info(f"CSV extraído: {csv_file}, {len(df)} linhas")
                return df

        except zipfile.BadZipFile:
            logger.error("Arquivo ZIP corrompido")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Erro ao extrair CSV: {type(e).__name__}: {e}")
            return pd.DataFrame()

    # =========================================================================
    # EXTRAÇÃO DE MÉTRICAS
    # =========================================================================

    def extract_metric(
        self,
        df_statement: pd.DataFrame,
        metric_key: str,
        cnpj_filter: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Extrai uma métrica específica de um DataFrame de demonstração financeira.
        
        Busca por CD_CONTA e DS_CONTA simultaneamente para máxima robustez.
        
        Args:
            df_statement: DataFrame da demonstração (BPA, BPP ou DRE)
            metric_key: Chave do CVM_ACCOUNT_MAP
            cnpj_filter: CNPJ para filtrar (8+ dígitos, será padronizado)
        
        Returns:
            DataFrame com as linhas correspondentes à métrica.
        """
        if df_statement.empty or metric_key not in CVM_ACCOUNT_MAP:
            return pd.DataFrame()

        config = CVM_ACCOUNT_MAP[metric_key]

        # Filtrar por CNPJ se especificado
        df = df_statement.copy()
        if cnpj_filter and "CNPJ_CIA" in df.columns:
            # Normalizar CNPJ (remover pontos/traços)
            df["_cnpj_clean"] = df["CNPJ_CIA"].str.replace(r"[./-]", "", regex=True)
            cnpj_clean = cnpj_filter.replace(".", "").replace("/", "").replace("-", "")
            df = df[df["_cnpj_clean"].str.startswith(cnpj_clean[:8])]
            df = df.drop(columns=["_cnpj_clean"])

        if df.empty:
            return pd.DataFrame()

        # Buscar por código de conta
        mask = pd.Series(False, index=df.index)
        match_mode = config.get("match_mode", "exact")  # default: match exato

        if "CD_CONTA" in df.columns:
            for pattern in config["cd_conta_patterns"]:
                # Match exato para evitar que "1" pegue "1.01", "1.02" etc.
                mask = mask | (df["CD_CONTA"].str.strip() == pattern)

        # Buscar por descrição de conta (complementar)
        if "DS_CONTA" in df.columns:
            for pattern in config["ds_conta_patterns"]:
                mask = mask | df["DS_CONTA"].str.contains(pattern, case=False, na=False)

        result = df[mask].copy()

        if result.empty:
            logger.debug(f"Métrica '{metric_key}' não encontrada no DataFrame")

        return result

    # =========================================================================
    # PIPELINE COMPLETO PARA UM BANCO
    # =========================================================================

    def extract_bank_metrics(
        self,
        cnpj_root: str,
        cvm_name_pattern: Optional[str],
        years: Optional[list[int]] = None,
        doc_types: Optional[list[str]] = None
    ) -> pd.DataFrame:
        """
        Extrai todas as métricas disponíveis de um banco via CVM.
        
        Args:
            cnpj_root: CNPJ raiz (8 dígitos)
            cvm_name_pattern: Padrão de nome na CVM (para log e verificação)
            years: Lista de anos (default: START_YEAR a END_YEAR)
            doc_types: Lista de tipos de documento (default: ["ITR", "DFP"])
        
        Returns:
            DataFrame consolidado com métricas por trimestre.
        """
        if years is None:
            # CVM mantém apenas os últimos ~5 anos de ITR/DFP.
            # Se START_YEAR for mais antigo, os anos excedentes vão retornar 404,
            # o que é tratado normalmente pelo _download_zip. Mas para evitar
            # chamadas desnecessárias, limitamos ao max(START_YEAR, corrente-5).
            from datetime import date as _date
            cvm_min_year = max(START_YEAR, _date.today().year - 5)
            years = list(range(cvm_min_year, END_YEAR + 1))
            logger.info(f"CVM: anos a processar: {years} (CVM mantém ~5 anos de histórico)")
        if doc_types is None:
            doc_types = ["ITR", "DFP"]

        all_records = []

        for year in years:
            for doc_type in doc_types:
                zip_content = self._download_zip(doc_type, year)
                if zip_content is None:
                    continue

                for metric_key, config in CVM_ACCOUNT_MAP.items():
                    statement = config["statement"]
                    df_statement = self._extract_csv_from_zip(zip_content, statement)

                    if df_statement.empty:
                        continue

                    df_metric = self.extract_metric(df_statement, metric_key, cnpj_root)

                    if df_metric.empty:
                        continue

                    # Processar resultados
                    for _, row in df_metric.iterrows():
                        dt_refer = row.get("DT_REFER", row.get("DT_FIM_EXERC", ""))
                        versao = row.get("VERSAO", "1")
                        vl_conta = row.get("VL_CONTA", "0")
                        escala = row.get("ESCALA_MOEDA", "UNIDADE")
                        denom = row.get("DENOM_CIA", "")

                        # Converter valor
                        try:
                            valor = float(vl_conta.replace(",", ".")) if isinstance(vl_conta, str) else float(vl_conta)
                        except (ValueError, TypeError):
                            logger.warning(f"Valor inválido para {metric_key}: '{vl_conta}'")
                            continue

                        # Padronizar escala para R$ mil (mesma unidade do IFData)
                        # CVM pode reportar em "MIL" (valor já em milhares) ou "UNIDADE" (valor em reais)
                        # IFData reporta em R$ mil.
                        # Resultado: tudo fica em R$ mil para comparabilidade.
                        if escala == "UNIDADE":
                            valor = valor / 1000  # Converter unidade → mil
                        # Se escala == "MIL", valor já está em mil, não mexe

                        record = {
                            "metric_name": metric_key,
                            "metric_value": valor,
                            "metric_unit": "R$",
                            "reference_date": dt_refer,
                            "doc_type": doc_type,
                            "version": versao,
                            "source_name": "CVM",
                            "source_table": f"{doc_type}_{statement}_con",
                            "source_field": f"{row.get('CD_CONTA', '')} - {row.get('DS_CONTA', '')}",
                            "denom_cia": denom,
                            "escala_original": escala,
                        }
                        all_records.append(record)

        if all_records:
            df = pd.DataFrame(all_records)
            # Manter apenas a versão mais recente para cada combinação
            if "version" in df.columns and "reference_date" in df.columns:
                df["version"] = pd.to_numeric(df["version"], errors="coerce").fillna(1)
                df = df.sort_values("version", ascending=False)
                df = df.drop_duplicates(
                    subset=["metric_name", "reference_date", "doc_type"],
                    keep="first"
                )
            logger.info(
                f"CVM: extraídos {len(df)} registros para CNPJ={cnpj_root} "
                f"({cvm_name_pattern})"
            )
            return df

        logger.warning(f"CVM: nenhum dado encontrado para CNPJ={cnpj_root}")
        return pd.DataFrame()
