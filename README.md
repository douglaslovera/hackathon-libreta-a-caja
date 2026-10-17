# Libreta → Caja

Libreta para bodegas peruanas: convierte mensajes y notas de voz en ventas,
gastos y fiados, consulta deudas y calcula el flujo semanal y una cuota orientativa.
Pregunta cuando falta un monto, sin inventar precios. Telegram funciona con long polling.

## Ejecutar localmente

Python 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edita .env: claves OpenAI/Telegram y DB_PATH=./libreta.db para uso local.
set -a
. ./.env
set +a
.venv/bin/python main.py
```

El archivo `.env` se carga con el comando anterior; la aplicación lee variables del entorno.
Crea el bot y obtén `TELEGRAM_TOKEN` con @BotFather. Nunca publiques las claves.
Las notas `.ogg` se envían directamente a OpenAI, sin ffmpeg.

Comandos: `/start`, `/flujo`, `/cuota`, `/resumen`, `/deudas`, `/reiniciar` (con confirmación).
Usa `/lote` y envía un CSV con columnas `id_mensaje,dia,mensaje`; también puedes
adjuntarlo con la leyenda `/lote` o responder al documento con ese comando.
Devuelve `entrega.csv` en una libreta separada del chat.

```sh
# Lote local (requiere OPENAI_API_KEY)
.venv/bin/python -m canales.lote entrada.csv salida.csv
# Evaluación
.venv/bin/python -m eval.evaluar
# Pruebas del canal, sin red ni claves
.venv/bin/pytest tests/test_voz.py tests/test_telegram.py
```

Telegram conserva los chats y la semana de recepción de los mensajes en tablas auxiliares
del mismo SQLite. Los reportes usan lunes a domingo, America/Lima, y el resumen automático
se envía los domingos a las 20:00. Los movimientos antiguos sin metadatos de Telegram se
consideran de la semana actual: el contrato de transacciones solo guarda el día, no la fecha.
La cuota incluye semanas vacías entre la primera registrada y la actual; es una orientación,
no una aprobación de crédito. Las correcciones conservan la semana del movimiento original.

## Railway

1. Crea un servicio desde este repositorio; Railway usará el `Dockerfile`.
2. Agrega un **Volume** montado en `/data`.
3. Configura `OPENAI_API_KEY`, `TELEGRAM_TOKEN`, `OPENAI_MODEL=gpt-6-luna`,
   `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe` y `DB_PATH=/data/libreta.db`.
4. Mantén **1 réplica**, como indica `railway.json`, y despliega.
5. No necesitas dominio público ni webhook: el bot consulta Telegram con long polling.

Dos instancias con el mismo token causan **Telegram 409 Conflict**. Detén el bot local
antes de usar Railway y evita que dos despliegues se solapen. El Volume mantiene la libreta
entre reinicios. La dependencia `python-telegram-bot[job-queue]` habilita el resumen semanal.
