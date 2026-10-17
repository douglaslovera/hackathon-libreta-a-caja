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
# Edita .env: OPENAI_API_KEY, API_KEY y DB_PATH=./libreta.db.
# TELEGRAM_TOKEN es opcional; omítelo para ejecutar solo la API.
.venv/bin/python main.py
```

La aplicación carga `.env` automáticamente; las variables del entorno tienen prioridad.
Sin `API_KEY` se rechaza el arranque, salvo con `ALLOW_NO_AUTH=1` para desarrollo local.
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
se envía los domingos a las 20:00. Los movimientos antiguos sin metadatos de semana se
consideran de la semana actual: el contrato de transacciones solo guarda el día, no la fecha.
La cuota incluye semanas vacías entre la primera registrada y la actual; es una orientación,
no una aprobación de crédito. Las correcciones conservan la semana del movimiento original.

## HTTP API

La API y Telegram comparten una instancia de `Libreta` y el mismo SQLite. Para ejecutar
solo la API (sin iniciar polling de Telegram):

```sh
.venv/bin/python main.py --no-telegram
# Desarrollo sin autenticación, solo en tu máquina:
ALLOW_NO_AUTH=1 API_KEY= TELEGRAM_TOKEN= .venv/bin/python main.py --no-telegram
```

Escucha en `0.0.0.0:$PORT` (por defecto 8000). [Swagger UI](http://localhost:8000/docs)
y [OpenAPI](http://localhost:8000/openapi.json) describen los cuerpos y respuestas.
Todas las rutas, incluidos los documentos, requieren `Authorization: Bearer <API_KEY>`;
solo `/health` es pública. Para abrir los documentos en el navegador, añade ese header
con tu cliente o usa el modo local `ALLOW_NO_AUTH=1` sin `API_KEY` configurada.

```sh
export API_KEY='tu-clave-local'
# Nota de voz: webm, ogg, m4a, mp3 o wav, máximo 10 MB.
curl -H "Authorization: Bearer $API_KEY" \
  -F 'file=@nota.webm;type=audio/webm' http://localhost:8000/shops/web:rosa/audio
# Resumen del dashboard.
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/shops/web:rosa/summary
# CSV de entrada: id_mensaje,dia,mensaje. Devuelve entrega.csv.
curl -H "Authorization: Bearer $API_KEY" -F 'file=@entrada.csv;type=text/csv' \
  -o entrega.csv -D - http://localhost:8000/shops/web:rosa/batch
```

También hay rutas por tienda para `pending` (consultar, responder con `POST pending/answer`
y omitir con `DELETE`), `transactions?week=all`, `debts`, `cash-flow?week=YYYY-MM-DD`,
`loan-estimate` y `DELETE /shops/{shop_id}` para reiniciar. Sin `week`, los reportes usan
la semana actual en Lima. Los identificadores admiten letras ASCII, números, `:`, `_`, `-`
y hasta 64 caracteres; `tg:<chat_id>` permite consultar la misma libreta de Telegram.
Las claves JSON están en inglés; tipos, productos y explicaciones conservan el español.
Los CSV de lote usan una libreta separada y exponen los headers `X-Rows`,
`X-Needs-Confirmation` y `X-Net-Cash-Flow`.

## Calling from Vercel/Next.js

Configura `LIBRETA_API_URL` y `LIBRETA_API_KEY` como variables **del servidor** en Vercel.
Nunca uses `NEXT_PUBLIC_` para la clave. Ejemplo con App Router en
`app/api/summary/route.ts`; protege esta ruta con la sesión de tu aplicación y obtiene
el identificador de tienda de esa sesión antes de habilitarla para varios usuarios:

```ts
export async function GET() {
  // Ejemplo de una sola tienda; en producción deriva shopId de la sesión autenticada.
  const shopId = "web:rosa";
  const response = await fetch(
    `${process.env.LIBRETA_API_URL}/shops/${encodeURIComponent(shopId)}/summary`,
    {
      headers: { Authorization: `Bearer ${process.env.LIBRETA_API_KEY}` },
      cache: "no-store",
    },
  );
  return new Response(await response.text(), {
    status: response.status,
    headers: { "Content-Type": "application/json" },
  });
}
```

El navegador llama a `/api/summary`; el servidor de Next.js añade la clave.

## Railway

1. Crea un servicio desde este repositorio; Railway usará el `Dockerfile`.
2. Agrega un **Volume** montado en `/data`.
3. Configura `OPENAI_API_KEY`, `API_KEY`, `OPENAI_MODEL=gpt-6-luna`,
   `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe` y `DB_PATH=/data/libreta.db`.
   Añade `TELEGRAM_TOKEN` si usarás el bot. Mantén `ALLOW_NO_AUTH=0`.
4. Configura `CORS_ORIGINS=https://your-app.vercel.app,http://localhost:3000`
   con tus orígenes reales, separados por comas.
5. Habilita un **dominio público** para que Next.js acceda a la API y configura
   `LIBRETA_API_URL` en Vercel con esa URL. Railway suministra `PORT`.
6. Mantén **1 réplica**, por SQLite y el polling de Telegram. El healthcheck
   configurado en `railway.json` consulta `/health`.

Dos instancias con el mismo token causan **Telegram 409 Conflict**. Detén el bot local
antes de usar Railway y evita que dos despliegues se solapen. El Volume mantiene la libreta
entre reinicios. La dependencia `python-telegram-bot[job-queue]` habilita el resumen semanal.
