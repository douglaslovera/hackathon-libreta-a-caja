"""Run the HTTP API and optional Telegram polling in one process."""
import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from canales.api import create_app
from core.memoria import abrir_memoria
from core.servicio import Libreta


def build_app(env=None, *, no_telegram=False):
    """Build storage and the app without starting Telegram polling; env mappings are injectable."""
    env = os.environ if env is None else env
    api_key = env.get('API_KEY', '').strip()
    if not api_key and env.get('ALLOW_NO_AUTH') != '1':
        raise ValueError('Falta API_KEY. Configura API_KEY o ALLOW_NO_AUTH=1 para desarrollo local.')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    destino = env.get('DATABASE_URL') or env.get('DB_PATH') or './libreta.db'
    if not destino.startswith(('postgres://', 'postgresql://')):
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
    memoria = abrir_memoria(destino)
    libreta = Libreta(memoria)
    app = create_app(libreta, api_key=api_key or None,
                     cors_origins=[s.strip() for s in env.get('CORS_ORIGINS', '').split(',') if s.strip()])
    original_lifespan = app.router.lifespan_context
    token = env.get('TELEGRAM_TOKEN', '').strip()

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original_lifespan(app):
                if token and not no_telegram:
                    from canales.telegram import crear_aplicacion

                    app_tg = crear_aplicacion(token, libreta)
                    await app_tg.initialize()
                    try:
                        await app_tg.start()
                        try:
                            await app_tg.updater.start_polling(drop_pending_updates=False)
                            try:
                                yield
                            finally:
                                await app_tg.updater.stop()
                        finally:
                            await app_tg.stop()
                    finally:
                        await app_tg.shutdown()
                else:
                    yield
        finally:
            await asyncio.to_thread(libreta.close)

    app.router.lifespan_context = lifespan
    app.state.libreta = libreta
    return app


def main():
    from core.config import cargar_env
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-telegram', action='store_true', help='Run only the HTTP API')
    args = parser.parse_args()
    cargar_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        app = build_app(no_telegram=args.no_telegram)
    except ValueError as exc:
        print(str(exc))
        return 1
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT') or '8000'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
