"""
Cliente para a API OData do IFData (Banco Central do Brasil).

Responsabilidades:
1. Descobrir códigos de instituição (CodInst) a partir do cadastro
2. Descobrir estrutura dos relatórios (ListaDeRelatorio)
3. Extrair dados brutos (IfDataValores) para bancos e períodos selecionados
4. Retornar DataFrames crus com metadados de rastreabilidade

A API possui 3 endpoints principais:
- IfDataCadastro: lista de instituições financeiras por período
- ListaDeRelatorio: metadados dos relatórios (nomes de colunas, tipos)
- IfDataValores: dados efetivos dos relatórios

Assinaturas confirmadas na API:
- IfDataCadastro(AnoMes=YYYYMM)
- ListaDeRelatorio()
- IfDataValores(AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)

Referência:
https://dadosabertos.bcb.gov.br/dataset/ifdata---dados-selecionados-de-instituies-financeiras
"""

import logging
import re
import time
import unicodedata
from typing import Optional, Union

import pandas as pd
import requests

from config.settings import (
    IFDATA_BASE_URL,
    IFDATA_TIMEOUT,
    IFDATA_MAX_RETRIES,
    IFDATA_RETRY_DELAY,
    DEFAULT_INSTITUTION_TYPE,
)

logger = logging.getLogger("bank_credit_pipeline.sources.ifdata")

IFDATA_PAGE_SIZE = 10000


class IFDataClient:
    """
    Cliente robusto para a API OData do IFData.

    Uso:
        client = IFDataClient()

        # Descobrir instituições
        cadastro = client.get_cadastro(periodo=202312)

        # Descobrir relatórios
        relatorios = client.get_report_list()

        # Extrair dados
        dados = client.get_report_data(periodo=202312, tipo_instituicao=2, relatorio="1")
    """

    def __init__(self):
        self.base_url = IFDATA_BASE_URL
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "BankCreditPipeline/1.0",
            }
        )

        # Cache de cadastro para evitar chamadas repetidas
        self._cadastro_cache: dict[tuple, pd.DataFrame] = {}

        # Cache de lista de relatórios
        self._report_list_cache: Optional[pd.DataFrame] = None

        # Cache de dados de relatórios: (periodo, tipo, relatorio) -> DataFrame
        self._report_data_cache: dict[tuple, pd.DataFrame] = {}

    # =========================================================================
    # MÉTODOS DE ACESSO À API
    # =========================================================================

    def _make_request(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        for attempt in range(1, IFDATA_MAX_RETRIES + 1):
            try:
                logger.debug(f"Requisição (tentativa {attempt}): {url}")
                if params:
                    logger.debug(f"Parâmetros: {params}")

                resp = self.session.get(url, params=params, timeout=IFDATA_TIMEOUT)
                logger.debug(f"URL final enviada: {resp.url}")

                if resp.status_code == 200:
                    return resp.json()

                logger.warning(
                    f"HTTP {resp.status_code} na tentativa {attempt}: {resp.url} | "
                    f"Resposta: {resp.text[:500]}"
                )

                if resp.status_code == 400:
                    logger.error("Erro 400 detectado. Não faz sentido retryar URI malformada.")
                    return None

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout na tentativa {attempt}: {url}")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Erro de conexão na tentativa {attempt}: {url} | {e}")
            except requests.exceptions.JSONDecodeError as e:
                logger.error(f"Resposta não é JSON válido: {url} | {e}")
                return None
            except Exception as e:
                logger.error(
                    f"Erro inesperado na tentativa {attempt}: {url} | {type(e).__name__}: {e}"
                )

            if attempt < IFDATA_MAX_RETRIES:
                logger.info(f"Aguardando {IFDATA_RETRY_DELAY}s antes de retry...")
                time.sleep(IFDATA_RETRY_DELAY)

        logger.error(f"Falha após {IFDATA_MAX_RETRIES} tentativas: {url}")
        return None

    def _build_odata_url(self, resource: str, **kwargs) -> str:
        """
        Constrói URL OData parametrizada para funções como IfDataValores.

        Exemplo:
            IfDataValores(AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)
            ?@AnoMes=202312&@TipoInstituicao=2&@Relatorio='1'&$format=json&$top=10000
        """
        if kwargs:
            param_defs = ",".join(f"{k}=@{k}" for k in kwargs)
            url = f"{self.base_url}/{resource}({param_defs})"
            query_parts = []
            for k, v in kwargs.items():
                if isinstance(v, str):
                    query_parts.append(f"@{k}='{v}'")
                else:
                    query_parts.append(f"@{k}={v}")
            query_parts.append("$format=json")
            query_parts.append(f"$top={IFDATA_PAGE_SIZE}")
            url += "?" + "&".join(query_parts)
        else:
            url = f"{self.base_url}/{resource}?$format=json&$top={IFDATA_PAGE_SIZE}"
        return url

    def _normalize_text(self, value: Optional[str]) -> str:
        """Normaliza texto para matching robusto."""
        if value is None:
            return ""

        value = str(value).strip().upper()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = re.sub(r"[^A-Z0-9]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _append_skip_to_url(self, url: str, skip: int) -> str:
        """Acrescenta $skip à URL OData."""
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}$skip={skip}"

    def _select_best_match(
        self,
        matches: pd.DataFrame,
        name_col: str,
        institution_type: int,
        primary_pattern: str,
    ) -> pd.Series:
        """
        Seleciona o melhor match entre múltiplas instituições candidatas.
        """

        df = matches.copy()
        df["_normalized_name"] = df[name_col].astype(str).map(self._normalize_text)
        normalized_primary = self._normalize_text(primary_pattern)

        df["_score"] = 0

        if "Situacao" in df.columns:
            df["_score"] += (df["Situacao"].astype(str).str.upper() == "A").astype(int) * 100

        # Preferência por prudencial ou individual, usando institution_type
        if institution_type == 2:
            df["_score"] += df["_normalized_name"].str.contains("PRUDENCIAL", na=False).astype(int) * 50
        elif institution_type == 3:
            df["_score"] += (~df["_normalized_name"].str.contains("PRUDENCIAL", na=False)).astype(int) * 50

        # Match exato é melhor que substring
        df["_score"] += (df["_normalized_name"] == normalized_primary).astype(int) * 80

        # Nome começando com o padrão é melhor que conter no meio
        df["_score"] += df["_normalized_name"].str.startswith(normalized_primary, na=False).astype(int) * 20

        # Tamanho menor tende a ser nome mais específico
        df["_name_len"] = df["_normalized_name"].str.len()

        df = df.sort_values(
            by=["_score", "_name_len"],
            ascending=[False, True],
        )

        return df.iloc[0]

    def _find_name_column(self, df: pd.DataFrame) -> Optional[str]:
        """Encontra a coluna de nome da instituição no DataFrame do cadastro."""
        candidates = ["NomeInstituicao", "Nome", "NomeInst", "Instituicao"]
        for col in df.columns:
            if col in candidates or "nome" in col.lower() or "instituicao" in col.lower():
                return col

        if len(df.columns) > 0:
            logger.debug(f"Colunas do cadastro: {df.columns.tolist()}")
        return None

    def _find_code_column(self, df: pd.DataFrame) -> Optional[str]:
        """Encontra a coluna de código da instituição no DataFrame."""
        exact_candidates = ["CodInst", "CodigoInstituicao", "CodInstituicao", "Codigo"]
        for col in df.columns:
            if col in exact_candidates:
                return col

        for col in df.columns:
            col_lower = col.lower()
            if "cod" in col_lower and ("inst" in col_lower or col_lower == "codigo"):
                return col

        exclude = ["relatorio", "moeda", "documento", "conta"]
        for col in df.columns:
            col_lower = col.lower()
            if "cod" in col_lower and not any(ex in col_lower for ex in exclude):
                return col

        return None

    # =========================================================================
    # CADASTRO DE INSTITUIÇÕES
    # =========================================================================

    def get_cadastro(
        self,
        periodo: int,
        tipo_instituicao: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Obtém cadastro de instituições financeiras para um período.

        Observação:
            A assinatura real da API para cadastro aceita apenas AnoMes.
            O argumento tipo_instituicao é mantido apenas por compatibilidade,
            mas não é usado na URL.

        Args:
            periodo: Data-base no formato YYYYMM
            tipo_instituicao: Ignorado pela API de cadastro, mantido por compatibilidade

        Returns:
            DataFrame com colunas do cadastro.
        """
        cache_key = (periodo,)
        if cache_key in self._cadastro_cache:
            return self._cadastro_cache[cache_key]

        if tipo_instituicao is not None:
            logger.debug(
                "tipo_instituicao informado em get_cadastro(), mas a API de cadastro "
                "aceita apenas AnoMes. O argumento será ignorado."
            )

        url = f"{self.base_url}/IfDataCadastro(AnoMes={periodo})?$format=json&$top={IFDATA_PAGE_SIZE}"
        data = self._make_request(url)

        if data is None or "value" not in data:
            logger.error(f"Falha ao obter cadastro para período={periodo}")
            return pd.DataFrame()

        df = pd.DataFrame(data["value"])
        logger.info(f"Cadastro obtido: período={periodo}, instituições={len(df)}")
        self._cadastro_cache[cache_key] = df
        return df

    def discover_bank_code(
        self,
        name_pattern: str,
        periodo: int,
        tipo_instituicao: int = DEFAULT_INSTITUTION_TYPE,
        name_alternatives: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        Descobre o CodInst de um banco a partir do padrão de nome.

        Estratégia:
        1. tenta match exato normalizado no padrão principal
        2. tenta match exato normalizado nos aliases
        3. tenta substring no padrão principal
        4. tenta substring nos aliases
        5. em caso de múltiplos matches, usa score considerando:
           - situação ativa
           - prudencial vs individual
           - exatidão do match

        Args:
            name_pattern: padrão principal para busca
            periodo: data-base YYYYMM
            tipo_instituicao: usado apenas para decidir melhor match
            name_alternatives: aliases adicionais

        Returns:
            CodInst como string, ou None.
        """
        df = self.get_cadastro(periodo)
        if df.empty:
            logger.warning(f"Cadastro vazio para período={periodo}.")
            return None

        name_col = self._find_name_column(df)
        code_col = self._find_code_column(df)

        if not name_col or not code_col:
            logger.error(
                f"Não foi possível encontrar colunas de nome/código no cadastro. "
                f"Colunas disponíveis: {df.columns.tolist()}"
            )
            return None

        patterns = [name_pattern]
        if name_alternatives:
            patterns.extend(name_alternatives)

        patterns = [p for p in patterns if p and str(p).strip()]
        normalized_patterns = list(dict.fromkeys(self._normalize_text(p) for p in patterns))

        df_work = df.copy()
        df_work["_normalized_name"] = df_work[name_col].astype(str).map(self._normalize_text)

        # 1. match exato
        exact_matches = df_work[df_work["_normalized_name"].isin(normalized_patterns)]
        if not exact_matches.empty:
            best = self._select_best_match(
                exact_matches,
                name_col=name_col,
                institution_type=tipo_instituicao,
                primary_pattern=name_pattern,
            )
            code = str(best[code_col])
            logger.info(
                f"Banco descoberto por match exato: '{name_pattern}' -> CodInst={code} "
                f"(nome='{best[name_col]}', período={periodo})"
            )
            return code

        # 2. substring match
        contains_mask = pd.Series(False, index=df_work.index)
        for p in normalized_patterns:
            contains_mask = contains_mask | df_work["_normalized_name"].str.contains(re.escape(p), na=False)

        contains_matches = df_work[contains_mask]
        if not contains_matches.empty:
            best = self._select_best_match(
                contains_matches,
                name_col=name_col,
                institution_type=tipo_instituicao,
                primary_pattern=name_pattern,
            )
            code = str(best[code_col])

            sample_names = contains_matches[name_col].astype(str).head(10).tolist()
            logger.warning(
                f"Múltiplos ou amplos matches para '{name_pattern}'. "
                f"Usando melhor candidato: CodInst={code}, nome='{best[name_col]}'. "
                f"Alguns candidatos: {sample_names}"
            )
            return code

        logger.warning(
            f"Banco '{name_pattern}' não encontrado no cadastro do período {periodo}. "
            f"Aliases testados: {patterns}"
        )
        return None

    # =========================================================================
    # LISTA DE RELATÓRIOS
    # =========================================================================

    def get_report_list(self) -> pd.DataFrame:
        """
        Obtém a lista de relatórios disponíveis.

        A assinatura correta da API é ListaDeRelatorio().
        """
        if self._report_list_cache is not None:
            return self._report_list_cache

        candidates = [
            f"{self.base_url}/ListaDeRelatorio()?$format=json",
            f"{self.base_url}/ListaDeRelatorio()?$format=json&$top=1000",
        ]

        for url in candidates:
            data = self._make_request(url)
            if data is not None and "value" in data:
                df = pd.DataFrame(data["value"])
                self._report_list_cache = df
                logger.info(f"Lista de relatórios obtida: {len(df)} entradas")
                return df

        logger.warning("ListaDeRelatorio indisponível. Seguindo sem discovery de relatórios.")
        return pd.DataFrame()

    # =========================================================================
    # EXTRAÇÃO DE DADOS DOS RELATÓRIOS
    # =========================================================================

    def get_report_data(
        self,
        periodo: int,
        tipo_instituicao: int,
        relatorio: str,
    ) -> pd.DataFrame:
        """
        Extrai dados de um relatório específico do IFData.

        Faz paginação automática com $skip quando o volume excede 10.000 linhas.
        """

        cache_key = (periodo, tipo_instituicao, relatorio)
        if cache_key in self._report_data_cache:
            logger.debug(f"Cache hit relatório: {cache_key}")
            return self._report_data_cache[cache_key]

        base_url = self._build_odata_url(
            "IfDataValores",
            AnoMes=periodo,
            TipoInstituicao=tipo_instituicao,
            Relatorio=relatorio,
        )

        all_rows = []
        skip = 0
        page = 1

        while True:
            paged_url = self._append_skip_to_url(base_url, skip) if skip > 0 else base_url
            data = self._make_request(paged_url)

            if data is None or "value" not in data:
                if page == 1:
                    logger.error(
                        f"Falha ao extrair relatório: período={periodo}, "
                        f"tipo={tipo_instituicao}, relatório={relatorio}"
                    )
                    empty = pd.DataFrame()
                    self._report_data_cache[cache_key] = empty
                    return empty

                logger.warning(
                    f"Paginação interrompida no relatório {relatorio}, "
                    f"período={periodo}, tipo={tipo_instituicao}, página={page}."
                )
                break

            batch = data["value"]
            batch_size = len(batch)

            logger.info(
                f"Relatório {relatorio}, período={periodo}, tipo={tipo_instituicao}, "
                f"página={page}, linhas={batch_size}"
            )

            if batch_size == 0:
                break

            all_rows.extend(batch)

            if batch_size < IFDATA_PAGE_SIZE:
                break

            skip += IFDATA_PAGE_SIZE
            page += 1

        df = pd.DataFrame(all_rows)
        logger.info(
            f"Relatório extraído: período={periodo}, tipo={tipo_instituicao}, "
            f"relatório={relatorio}, linhas_totais={len(df)}"
        )
        self._report_data_cache[cache_key] = df
        return df

    def extract_bank_data(
        self,
        bank_code: Union[str, int],
        periodo: int,
        tipo_instituicao: int,
        relatorio: str,
    ) -> pd.DataFrame:
        """
        Extrai dados de um relatório filtrados para um banco específico.
        """
        df = self.get_report_data(periodo, tipo_instituicao, relatorio)
        if df.empty:
            return df

        code_col = self._find_code_column(df)
        if code_col is None:
            logger.error(
                f"Coluna de código não encontrada no relatório {relatorio}. "
                f"Colunas disponíveis: {df.columns.tolist()}"
            )
            return pd.DataFrame()

        mask = df[code_col].astype(str) == str(bank_code)
        result = df[mask]

        if result.empty:
            logger.warning(
                f"Banco CodInst={bank_code} não encontrado no relatório {relatorio}, "
                f"período={periodo}"
            )

        return result

    # =========================================================================
    # EXTRAÇÃO EM LOTE
    # =========================================================================

    def extract_all_banks_period(
        self,
        bank_codes: dict[str, Union[str, int]],
        periodo: int,
        tipo_map: dict[str, int],
        relatorio: str,
    ) -> pd.DataFrame:
        """
        Extrai dados de um relatório para todos os bancos em um período.

        Estratégia otimizada:
        puxa o relatório inteiro por tipo e filtra localmente.
        """
        all_data = []

        by_type: dict[int, list[tuple[str, Union[str, int]]]] = {}
        for bank_id, code in bank_codes.items():
            tipo = tipo_map.get(bank_id, DEFAULT_INSTITUTION_TYPE)
            by_type.setdefault(tipo, []).append((bank_id, code))

        for tipo, banks in by_type.items():
            df_full = self.get_report_data(periodo, tipo, relatorio)
            if df_full.empty:
                for bank_id, code in banks:
                    logger.warning(
                        f"Sem dados: banco={bank_id}, período={periodo}, "
                        f"tipo={tipo}, relatório={relatorio}"
                    )
                continue

            code_col = self._find_code_column(df_full)
            if code_col is None:
                logger.error(
                    f"Coluna de código não encontrada. Colunas: {df_full.columns.tolist()}"
                )
                continue

            for bank_id, code in banks:
                mask = df_full[code_col].astype(str) == str(code)
                df_bank = df_full[mask].copy()

                if df_bank.empty:
                    logger.warning(
                        f"Banco {bank_id} (CodInst={code}) não encontrado no "
                        f"relatório {relatorio}, período={periodo}, tipo={tipo}"
                    )
                else:
                    df_bank["bank_id"] = bank_id
                    df_bank["reference_period"] = periodo
                    df_bank["institution_type"] = tipo
                    all_data.append(df_bank)

        if all_data:
            return pd.concat(all_data, ignore_index=True)

        return pd.DataFrame()