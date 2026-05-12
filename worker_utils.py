"""
worker_utils.py — Utilitários compartilhados pelos workers daemon
=================================================================
Implementa **circuit breaker** simples para evitar que workers fiquem
batendo em endpoints/sites quebrados em loop ou poluindo logs.

Uso típico em um worker:

    from worker_utils import CircuitBreaker

    _cb = CircuitBreaker("url_health", logger=logger)

    def _loop():
        while True:
            if _cb.deve_pausar():
                time.sleep(min(30, _cb.segundos_restantes()))
                continue
            try:
                _tick()
                _cb.registrar_sucesso()
            except Exception as e:
                _cb.registrar_falha(e)
            time.sleep(TICK_SEGUNDOS)

Política:
- Após 3 falhas consecutivas: pausa 5 min (backoff exponencial: 5/10/20/60 min)
- 1 sucesso reseta o contador e libera o worker
- Loga ALERT quando entra em pausa e quando se recupera
"""

from __future__ import annotations

import logging
import threading
import time


class CircuitBreaker:
    """Circuit breaker simples por worker. Thread-safe."""

    LIMIAR_FALHAS = 3        # falhas seguidas antes de abrir o circuito
    BACKOFFS = [300, 600, 1200, 3600]   # 5min, 10min, 20min, 1h (máx)

    def __init__(self, nome: str, logger: logging.Logger | None = None) -> None:
        self.nome = nome
        self._logger = logger or logging.getLogger(f"worker.{nome}")
        self._lock = threading.Lock()
        self._falhas_seguidas = 0
        self._pausa_ate = 0.0       # epoch seconds
        self._ultima_falha: str = ""
        self._em_pausa_logado = False

    # ----- estado -----

    def deve_pausar(self) -> bool:
        with self._lock:
            return time.time() < self._pausa_ate

    def segundos_restantes(self) -> int:
        with self._lock:
            return max(0, int(self._pausa_ate - time.time()))

    def estado(self) -> dict:
        with self._lock:
            return {
                "nome":             self.nome,
                "falhas_seguidas":  self._falhas_seguidas,
                "em_pausa":         time.time() < self._pausa_ate,
                "pausa_restante_s": max(0, int(self._pausa_ate - time.time())),
                "ultima_falha":     self._ultima_falha,
            }

    # ----- callbacks -----

    def registrar_sucesso(self) -> None:
        with self._lock:
            houve_pausa = self._falhas_seguidas >= self.LIMIAR_FALHAS or self._em_pausa_logado
            self._falhas_seguidas = 0
            self._pausa_ate = 0.0
            self._ultima_falha = ""
            self._em_pausa_logado = False
        if houve_pausa:
            self._logger.warning(f"[CB:{self.nome}] RECUPERADO — circuito fechado, retomando ticks normais.")

    def registrar_falha(self, exc: BaseException | str) -> None:
        msg = str(exc) if exc else "?"
        with self._lock:
            self._falhas_seguidas += 1
            self._ultima_falha = msg[:200]
            if self._falhas_seguidas >= self.LIMIAR_FALHAS:
                idx = min(self._falhas_seguidas - self.LIMIAR_FALHAS, len(self.BACKOFFS) - 1)
                pausa = self.BACKOFFS[idx]
                self._pausa_ate = time.time() + pausa
                should_log = not self._em_pausa_logado
                self._em_pausa_logado = True
            else:
                pausa = 0
                should_log = False
        if pausa:
            if should_log:
                self._logger.error(
                    f"[CB:{self.nome}] ALERT — {self._falhas_seguidas} falhas seguidas. "
                    f"Pausando worker por {pausa//60}min. Última: {msg[:120]}"
                )
        else:
            self._logger.warning(f"[CB:{self.nome}] falha #{self._falhas_seguidas}: {msg[:120]}")
