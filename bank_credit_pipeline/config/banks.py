"""
Tabela mestre de bancos brasileiros.

Cada banco é identificado por múltiplos códigos usados em diferentes fontes.
- ifdata_code: código interno do IFData (CodInst). Pode ser alfanumérico e PRECISA SER VALIDADO.
- cnpj: CNPJ raiz (8 dígitos) usado como chave alternativa.
- cvm_code: código CVM (CD_CVM) para busca em ITR/DFP. Precisa ser validado.
- ticker: ticker na B3 (para referência e filtragem).
- ifdata_name_pattern: padrão principal de busca no nome retornado pela API IFData.
- ifdata_name_alternatives: aliases adicionais para discovery mais robusto.
- cvm_name_pattern: padrão principal de busca no nome retornado nos arquivos CVM.
- cvm_name_alternatives: aliases adicionais para busca na CVM.
- institution_type: tipo preferencial no IFData para IfDataValores (2=cong. prudencial, 3=individual).
- notes: observações sobre diferenças entre individual e conglomerado.

IMPORTANTE:
- ifdata_code e cvm_code seguem sendo validados dinamicamente.
- O endpoint IfDataCadastro usa apenas AnoMes, sem TipoInstituicao.
- O discovery por nome precisa usar padrões mais específicos para evitar matches errados.
"""

from typing import Optional
from dataclasses import dataclass, field


@dataclass
class BankConfig:
    """Configuração de um banco para o pipeline."""
    name: str
    ifdata_name_pattern: str
    cnpj_root: str
    ticker: Optional[str] = None
    cvm_name_pattern: Optional[str] = None
    ifdata_name_alternatives: list[str] = field(default_factory=list)
    cvm_name_alternatives: list[str] = field(default_factory=list)
    cvm_code: Optional[int] = None
    ifdata_code: Optional[str] = None
    institution_type: int = 2
    is_listed: bool = True
    notes: str = ""


# =============================================================================
# TABELA MESTRE, 13 bancos prioritários
# =============================================================================
# Regras adotadas:
# 1. ifdata_name_pattern ficou mais específico para melhorar o discovery.
# 2. ifdata_code agora é string, porque o IFData pode retornar códigos alfanuméricos.
# 3. institution_type foi mantido como preferência para IfDataValores.
# 4. ifdata_name_alternatives servirá para uma segunda passada de matching.
# =============================================================================

BANKS_MASTER = {
    "ITAU": BankConfig(
        name="Itaú Unibanco",
        ifdata_name_pattern="ITAU - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO ITAU UNIBANCO S.A.",
            "ITAU UNIBANCO",
            "ITAU",
        ],
        cnpj_root="60701190",
        ticker="ITUB4",
        cvm_name_pattern="ITAUUNIBANCO",
        cvm_name_alternatives=[
            "ITAU UNIBANCO",
            "ITAU",
        ],
        is_listed=True,
        institution_type=2,
        notes="Priorizar conglomerado prudencial no IFData.",
    ),
    "BRADESCO": BankConfig(
        name="Bradesco",
        ifdata_name_pattern="BRADESCO - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO BRADESCO S.A.",
            "BRADESCO",
        ],
        cnpj_root="60746948",
        ticker="BBDC4",
        cvm_name_pattern="BRADESCO",
        cvm_name_alternatives=[
            "BANCO BRADESCO",
        ],
        is_listed=True,
        institution_type=2,
        notes="Priorizar conglomerado prudencial no IFData.",
    ),
    "SANTANDER": BankConfig(
        name="Santander Brasil",
        ifdata_name_pattern="BANCO SANTANDER (BRASIL) S.A.",
        ifdata_name_alternatives=[
            "SANTANDER - PRUDENCIAL",
            "SANTANDER",
        ],
        cnpj_root="90400888",
        ticker="SANB11",
        cvm_name_pattern="SANTANDER",
        cvm_name_alternatives=[
            "BANCO SANTANDER",
        ],
        is_listed=True,
        institution_type=2,
        notes="Manter fallback para versão prudencial se existir no cadastro.",
    ),
    "BB": BankConfig(
        name="Banco do Brasil",
        ifdata_name_pattern="BANCO DO BRASIL S.A.",
        ifdata_name_alternatives=[
            "BB - PRUDENCIAL",
            "BANCO DO BRASIL",
        ],
        cnpj_root="00000000",
        ticker="BBAS3",
        cvm_name_pattern="BANCO DO BRASIL",
        cvm_name_alternatives=[
            "BCO BRASIL",
            "BBAS",
        ],
        is_listed=True,
        institution_type=2,
        notes="Evitar usar 'BCO DO BRASIL' como padrão principal, pois gera falso negativo.",
    ),
    "BMG": BankConfig(
        name="Banco BMG",
        ifdata_name_pattern="BMG - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO BMG S.A.",
            "BMG",
        ],
        cnpj_root="61186680",
        ticker="BMGB4",
        cvm_name_pattern="BMG",
        cvm_name_alternatives=[
            "BANCO BMG",
        ],
        is_listed=True,
        institution_type=2,
        notes="Preferir conglomerado prudencial no IFData.",
    ),
    "PINE": BankConfig(
        name="Banco Pine",
        ifdata_name_pattern="PINE - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO PINE S.A.",
            "PINE",
        ],
        cnpj_root="62144175",
        ticker="PINE4",
        cvm_name_pattern="PINE",
        cvm_name_alternatives=[
            "BANCO PINE",
        ],
        is_listed=True,
        institution_type=2,
        notes="Preferir conglomerado prudencial no IFData.",
    ),
    "INTER": BankConfig(
        name="Banco Inter",
        ifdata_name_pattern="BANCO INTER S.A.",
        ifdata_name_alternatives=[
            "INTER - PRUDENCIAL",
            "INTER",
        ],
        cnpj_root="18945670",
        ticker="INBR32",
        cvm_name_pattern="INTER",
        cvm_name_alternatives=[
            "BANCO INTER",
        ],
        is_listed=True,
        institution_type=2,
        notes="Não usar só 'INTER' como padrão principal, pois é amplo demais.",
    ),
    "BANRISUL": BankConfig(
        name="Banrisul",
        ifdata_name_pattern="BANCO DO ESTADO DO RIO GRANDE DO SUL S.A.",
        ifdata_name_alternatives=[
            "BANRISUL - PRUDENCIAL",
            "BANRISUL",
        ],
        cnpj_root="92702067",
        ticker="BRSR6",
        cvm_name_pattern="BANRISUL",
        cvm_name_alternatives=[
            "BANCO DO ESTADO DO RIO GRANDE DO SUL",
        ],
        is_listed=True,
        institution_type=2,
        notes="Banco estatal do RS.",
    ),
    "ABC_BRASIL": BankConfig(
        name="Banco ABC Brasil",
        ifdata_name_pattern="ABC-BRASIL - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO ABC BRASIL S.A.",
            "ABC BRASIL",
            "ABC-BRASIL",
        ],
        cnpj_root="28195667",
        ticker="ABCB4",
        cvm_name_pattern="ABC BRASIL",
        cvm_name_alternatives=[
            "BANCO ABC BRASIL",
            "ABC-BRASIL",
        ],
        is_listed=True,
        institution_type=2,
        notes="No IFData apareceu explicitamente como 'ABC-BRASIL - PRUDENCIAL'.",
    ),
    "BTG": BankConfig(
        name="BTG Pactual",
        ifdata_name_pattern="BTG PACTUAL - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO BTG PACTUAL S.A.",
            "BTG PACTUAL",
        ],
        cnpj_root="30306294",
        ticker="BPAC11",
        cvm_name_pattern="BTG PACTUAL",
        cvm_name_alternatives=[
            "BANCO BTG PACTUAL",
        ],
        is_listed=True,
        institution_type=2,
        notes="Preferir conglomerado prudencial no IFData.",
    ),
    "NUBANK": BankConfig(
        name="Nu Holdings / Nubank",
        ifdata_name_pattern="NU FINANCEIRA S.A.",
        ifdata_name_alternatives=[
            "NU FINANCEIRA",
            "NU PAGAMENTOS S.A.",
            "NUBANK",
        ],
        cnpj_root="18236120",
        ticker="NUBR33",
        cvm_name_pattern="NU HOLDINGS",
        cvm_name_alternatives=[
            "NU FINANCEIRA",
            "NUBANK",
        ],
        is_listed=True,
        institution_type=3,
        notes=(
            "Manter institution_type=3 por enquanto. "
            "Pode haver diferença entre holding listada e entidade operacional no IFData."
        ),
    ),
    "DAYCOVAL": BankConfig(
        name="Banco Daycoval",
        ifdata_name_pattern="BANCO DAYCOVAL S.A.",
        ifdata_name_alternatives=[
            "DAYCOVAL - PRUDENCIAL",
            "DAYCOVAL",
        ],
        cnpj_root="62232889",
        ticker="DAYC4",
        cvm_name_pattern="DAYCOVAL",
        cvm_name_alternatives=[
            "BANCO DAYCOVAL",
        ],
        is_listed=True,
        institution_type=2,
        notes="Banco de middle market, forte em crédito corporativo e câmbio.",
    ),
    "AGIBANK": BankConfig(
        name="Agibank",
        ifdata_name_pattern="AGIBANK - PRUDENCIAL",
        ifdata_name_alternatives=[
            "BANCO AGIBANK S.A.",
            "AGIBANK",
        ],
        cnpj_root="07207996",
        ticker=None,
        cvm_name_pattern=None,
        cvm_name_alternatives=[],
        is_listed=False,
        institution_type=3,
        notes="Banco digital, não listado em bolsa. Validar se o melhor corte é individual ou prudencial.",
    ),
}


def get_bank_ids() -> list[str]:
    """Retorna lista de IDs dos bancos configurados."""
    return list(BANKS_MASTER.keys())


def get_listed_banks() -> dict[str, BankConfig]:
    """Retorna apenas bancos listados, que podem ser buscados na CVM."""
    return {k: v for k, v in BANKS_MASTER.items() if v.is_listed}