"""
Configuração centralizada de logging do pipeline.

Gera logs em dois destinos:
- Console (INFO+): para acompanhamento em tempo real
- Arquivo (DEBUG+): para auditoria completa

Formato do log inclui timestamp, nível, módulo e mensagem.
"""

import logging
import os
from config.settings import OUTPUT_DIR


def setup_logging(log_level: str = "DEBUG") -> logging.Logger:
    """
    Configura e retorna o logger raiz do pipeline.
    
    Args:
        log_level: Nível mínimo de logging para o arquivo (default: DEBUG).
    
    Returns:
        Logger configurado.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_file = os.path.join(OUTPUT_DIR, "pipeline.log")

    logger = logging.getLogger("bank_credit_pipeline")
    logger.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))

    # Limpa handlers existentes (evita duplicação em re-runs)
    logger.handlers.clear()

    # Formato detalhado
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s.%(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler de arquivo (DEBUG+)
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Handler de console (INFO+)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger
