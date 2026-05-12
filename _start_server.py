"""
_start_server.py — Ponto de entrada do AMBIENTE DE DESENVOLVIMENTO (Waitress WSGI)
==================================================================================
**Este é o fork `prospector-bets-dev`** — roda na porta 5003 para coexistir com
o sistema principal (`projeto bet`) que roda em 5002.

Usar:
    python _start_server.py [--host 127.0.0.1] [--port 5003] [--threads 4]
"""

import sys
import os
import argparse

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

parser = argparse.ArgumentParser(description="Prospector Bets DEV — servidor WSGI")
parser.add_argument("--host",    default="127.0.0.1", help="Endereço de bind (padrão: 127.0.0.1)")
parser.add_argument("--port",    default=5003, type=int, help="Porta (padrão: 5003 — fork DEV)")
parser.add_argument("--threads", default=4,    type=int, help="Threads Waitress (padrão: 4)")
args = parser.parse_args()

from app import app

try:
    from waitress import serve
    import waitress as _w
    _wver = getattr(_w, '__version__', None) or getattr(_w, 'version', None) or 'ok'
    print(f"[server] Waitress {_wver} · "
          f"http://{args.host}:{args.port} · {args.threads} threads")
    serve(app, host=args.host, port=args.port, threads=args.threads,
          channel_timeout=60, cleanup_interval=30)
except ImportError:
    # Fallback gracioso para Werkzeug caso Waitress não esteja instalado
    print("[server] Waitress não encontrado — usando servidor Flask dev (não recomendado para produção)")
    app.run(debug=False, port=args.port, use_reloader=False, host=args.host)
