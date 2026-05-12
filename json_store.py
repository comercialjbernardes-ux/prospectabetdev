"""
json_store.py — I/O seguro para arquivos JSON com escrita atômica e backup.

Substitui os json.loads/json.dumps diretos nos módulos, garantindo:
  - Escrita atômica (write to .tmp → rename) — sem arquivos meio-escritos
  - Backup automático da versão anterior (.bak)
  - Lock por arquivo — thread safety entre workers
  - Recuperação automática via backup em caso de corrupção
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Um lock por caminho de arquivo — evita escrita simultânea entre threads
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_META = threading.Lock()


def _get_lock(caminho: Path) -> threading.RLock:
    key = str(caminho.resolve())
    with _LOCKS_META:
        if key not in _LOCKS:
            _LOCKS[key] = threading.RLock()
        return _LOCKS[key]


def ler(caminho: str | Path, default: Any = None) -> Any:
    """
    Lê JSON com fallback para backup se o arquivo estiver corrompido.
    Retorna `default` se o arquivo não existir ou não puder ser recuperado.
    """
    caminho = Path(caminho)
    with _get_lock(caminho):
        if not caminho.exists():
            return default

        try:
            return json.loads(caminho.read_text("utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"JSON corrompido em {caminho}: {e}")
            backup = caminho.with_suffix(".json.bak")
            if backup.exists():
                try:
                    data = json.loads(backup.read_text("utf-8"))
                    logger.warning(f"Recuperado de backup: {backup}")
                    return data
                except Exception:
                    pass
            return default
        except OSError as e:
            logger.error(f"Erro ao ler {caminho}: {e}")
            return default


def salvar(caminho: str | Path, data: Any, criar_backup: bool = True) -> bool:
    """
    Salva JSON com escrita atômica e backup automático.

    Fluxo:
      1. Serializa para string
      2. Escreve em arquivo .tmp
      3. Copia arquivo atual para .bak (se criar_backup=True e arquivo existe)
      4. Rename atômico .tmp → destino final
    Retorna True em sucesso, False em falha.
    """
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".json.tmp")

    with _get_lock(caminho):
        try:
            conteudo = json.dumps(data, ensure_ascii=False, indent=2)
            tmp.write_text(conteudo, encoding="utf-8")

            if criar_backup and caminho.exists():
                shutil.copy2(caminho, caminho.with_suffix(".json.bak"))

            tmp.replace(caminho)
            return True

        except Exception as e:
            logger.error(f"Falha ao salvar {caminho}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False
