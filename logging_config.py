"""
logging_config.py — Configuração centralizada de logging estruturado (JSON).

Uso:
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("mensagem", extra={"cnpj": "12345"})

Arquivo de log: logs/app.log (rotação: 10 MB, 5 backups)
Console: nível WARNING em produção, DEBUG se LOG_LEVEL=DEBUG
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

_configurado = False


class JsonFormatter(logging.Formatter):
    """Formata cada registro de log como uma linha JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entrada: dict = {
            "ts": datetime.utcfromtimestamp(record.created).isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Inclui campos extras passados via `extra={...}`
        for chave, valor in record.__dict__.items():
            if chave not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                entrada[chave] = valor
        if record.exc_info:
            entrada["exc"] = self.formatException(record.exc_info)
        return json.dumps(entrada, ensure_ascii=False, default=str)


def configurar_logging(nivel_console: str | None = None) -> None:
    """
    Configura logging global uma única vez.

    - Console: WARNING (ou valor de LOG_LEVEL env / parâmetro)
    - Arquivo rotativo: DEBUG (captura tudo)
    """
    global _configurado
    if _configurado:
        return
    _configurado = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    nivel_env = (nivel_console or os.environ.get("LOG_LEVEL", "WARNING")).upper()
    nivel_num = getattr(logging, nivel_env, logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Handler de console — texto legível, nível configurável
    console_handler = logging.StreamHandler()
    console_handler.setLevel(nivel_num)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                          datefmt="%H:%M:%S")
    )

    # Handler de arquivo — JSON rotativo
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())

    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Silencia loggers barulhentos de bibliotecas
    for lib in ("urllib3", "requests", "werkzeug", "charset_normalizer"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(nome: str) -> logging.Logger:
    """Retorna logger configurado. Chama configurar_logging() se necessário."""
    configurar_logging()
    return logging.getLogger(nome)
