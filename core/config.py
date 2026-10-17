"""Load ./.env into os.environ for local runs. Real env vars (e.g. Railway's) always win."""

from __future__ import annotations

import os
from pathlib import Path


def cargar_env(path: str | Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    path = Path(path)
    if not path.is_file():
        return
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.removeprefix("export ").split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip("'\""))
