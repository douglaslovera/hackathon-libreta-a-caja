"""Arranque del bot con long polling: python main.py."""
import logging
import os
from pathlib import Path


def main():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        print("Falta TELEGRAM_TOKEN. Crea tu bot con @BotFather y exporta TELEGRAM_TOKEN antes de ejecutar python main.py.")
        return 1
    from core.memoria import Memoria
    from canales.telegram import crear_aplicacion

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP request URLs include the bot token; keep httpx request logging quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db_path = os.getenv("DB_PATH", "./libreta.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    memoria = Memoria(db_path)
    crear_aplicacion(token, memoria, db_path=db_path).run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
