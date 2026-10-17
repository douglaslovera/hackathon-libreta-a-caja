# Libreta → Caja: architecture and contracts

Hackathon (Mibanco, "De la libreta a la caja"). Doña Rosa runs a small shop in Peru and sends
WhatsApp-style texts/voice notes. We turn them into transactions, ask about missing data, track
debts (fiados), compute weekly cash flow and a sustainable monthly loan payment.
Challenge kit: `Kit_Reto_Mibanco/` (`ejemplos_mensajes.csv` input, `ejemplos_respuestas.csv` expected output).

**Golden rule: accurate records over convincing answers.** Extra rows or invented amounts lose points.
Never guess a price. When an amount is missing → row with empty monto and requiere_confirmacion=SI.

## Stack (decided, do not change)
- Python 3.12, venv at `.venv` (`.venv/bin/python`, `.venv/bin/pytest`).
- LLM: OpenAI `gpt-6-luna` via the `openai` SDK (env `OPENAI_MODEL`, default `gpt-6-luna`), structured
  JSON output, temperature 0. Key in env `OPENAI_API_KEY` (not available on the dev machine yet — every
  LLM call must be injectable/mockable so tests run offline).
- Voice: OpenAI `gpt-4o-mini-transcribe`, `language="es"`, `prompt` = shop vocabulary.
- Channel: Telegram via `python-telegram-bot` v22 (async, long polling). Env `TELEGRAM_TOKEN`.
- Storage: Supabase Postgres via psycopg 3 / psycopg_pool, selected by `DATABASE_URL`.
  SQLite (stdlib `sqlite3`, WAL) remains the offline/test fallback via `DB_PATH` (default `./libreta.db`).
  Postgres pool: 1–5 connections, prepared statements disabled for pooler compatibility. Single replica
  for Telegram polling; Railway needs no Volume when `DATABASE_URL` is configured.
- Hosting: Railway.

## Layout and ownership
```
core/modelos.py      shared dataclasses (Accion, Transaccion, Deuda, Contexto, Resultado) — FROZEN
core/procesar.py     procesar_mensaje(): glue  (lead)
core/agente.py       interpretar(): LLM → list[Accion]           (agent A)
core/prompt.py       system prompt (rule-based, general)          (agent A)
core/memoria.py      MemoriaSQLite, legacy Memoria alias, abrir_memoria factory
core/memoria_pg.py   MemoriaPostgres (same interface)
db/schema.sql       idempotent Postgres schema, applied on startup
core/verificacion.py amount-traceability guard                    (agent B)
core/ejecutor.py     ejecutar(): validate + apply actions         (agent B)
core/finanzas.py     flujo_semanal(), cuota_sostenible()          (agent B)
core/voz.py          transcribir()                                (agent C)
canales/telegram.py  bot                                          (agent C)
canales/lote.py      CSV in → CSV out                             (lead)
eval/evaluar.py      score vs kit examples                        (lead)
tests/               pytest, offline, one file per module
```

## Interfaces

```python
# core/agente.py
def interpretar(texto: str, contexto: Contexto, *, cliente=None, modelo: str | None = None) -> list[Accion]
#   cliente: an openai.OpenAI-compatible object (injected in tests). Returns [] only if the message has
#   nothing at all; an explicit `ignorar` action is preferred for non-business messages.

# core/memoria.py
def abrir_memoria(url: str | None = None)  # explicit URL/path, DATABASE_URL, DB_PATH, ./libreta.db
class MemoriaSQLite:  # Memoria alias preserved; MemoriaPostgres(dsn) implements the same methods
    def __init__(self, path: str = ":memory:") -> None
    def agregar(self, t: Transaccion) -> Transaccion              # sets t.id
    def actualizar(self, transaccion_id: int, **campos) -> Transaccion
    def obtener(self, transaccion_id: int) -> Transaccion | None
    def transacciones(self, tienda_id: str) -> list[Transaccion]  # insertion order
    def ultimas(self, tienda_id: str, n: int = 10) -> list[Transaccion]
    def deudas_abiertas(self, tienda_id: str) -> list[Deuda]
    def registrar_cobro(self, tienda_id: str, deuda_transaccion_id: int, monto: float | None) -> None
    #   monto None = paid in full. Reduces pendiente; closes the debt at 0.
    def buscar(self, tienda_id: str, texto: str, n: int = 5) -> list[Transaccion]   # producto/cliente: SQLite FTS5; Postgres tsvector + ILIKE
    def alias(self, tienda_id: str) -> dict[str, str]
    def guardar_alias(self, tienda_id: str, variante: str, canonico: str) -> None
    def pendiente(self, tienda_id: str) -> dict | None            # open follow-up question state
    def fijar_pendiente(self, tienda_id: str, datos: dict | None) -> None
    def log(self, tienda_id: str, id_mensaje: str, texto: str, salida_llm: str, resultado: Resultado) -> None
    def borrar_tienda(self, tienda_id: str) -> None
    def contexto(self, tienda_id: str, dia: str | None = None) -> Contexto
    def fechar_semana(self, tienda_id: str, id_mensaje: str, semana: str) -> None  # keep first date
    def semanas_mensaje(self, tienda_id: str) -> dict[str, str]
    def recordar_chat(self, chat_id: int) -> None
    def chats(self) -> list[int]
    def close(self) -> None

# core/ejecutor.py
def ejecutar(acciones: list[Accion], texto: str, *, tienda_id: str, id_mensaje: str,
             dia: str | None, memoria: Memoria) -> Resultado

# core/verificacion.py
def numeros_en_texto(texto: str) -> list[float]      # digits ("0.30", "4,20", "c/u") and Spanish words
                                                     # ("dos", "cuatro soles", "seis cincuenta" = 6.50)
def monto_trazable(monto: float, texto: str, extra: list[float] = ()) -> bool
#   True if monto equals (±0.005) a number in the text, a product/sum/difference of up to 3 of them,
#   or one of `extra` (e.g. pending debt amounts).

# core/finanzas.py
def flujo_semanal(transacciones: list[Transaccion]) -> dict
#   {"ventas", "cobros", "gastos", "flujo", "fiado_pendiente", "sin_monto": n} — flujo = ventas + cobros − gastos.
#   fiado does NOT count until collected. Rows with monto None are excluded and counted in sin_monto.
def cuota_sostenible(flujos_semanales: list[float], porcentaje: float = 0.30) -> dict
#   {"flujo_semanal_promedio", "flujo_mensual" (×4.33), "cuota", "porcentaje", "explicacion"} — Spanish
#   explanation, always ends with a line saying it is NOT a credit approval. cuota 0 if flujo ≤ 0.

# core/voz.py
def transcribir(audio: bytes, nombre_archivo: str = "nota.ogg", vocabulario: list[str] = (), *, cliente=None) -> str

# core/procesar.py
def procesar_mensaje(texto: str, *, tienda_id: str, id_mensaje: str, dia: str | None,
                     memoria: Memoria, interpretar=agente.interpretar) -> Resultado
```

## Domain rules (from the kit — the prompt must teach these as general rules, not memorized cases)
- **tipo**: `venta` (sale paid now), `gasto` (business expense, supplier, services, wages, refunds to
  customers), `fiado` (sale on credit — record customer), `cobro_fiado` (customer pays a debt).
- **One row per product** in a message ("10 panes a 0.30 y 3 leches a 4.20" → 2 rows).
- **monto = total**: cantidad × unit price ("a 0.30" / "c/u" / "el kilo" are unit prices); minus stated
  discounts ("a 190, me rebajó 10" → 180). Expenses/lump sums with no count → cantidad 1.
- **Corrections inside a message** ("5 panes, no perdón, fueron 8") → only the final version.
- **Corrections of earlier messages** → `corregir` the earlier transaction; no new row.
- **Lump sums** ("salieron 210 en ventas sueltas", "aparte de lo anotado 150 en abarrotes") → one venta,
  cantidad 1, producto as said (`ventas sueltas`, `abarrotes sueltos`).
- **Missing amount** ("vendí 3 atunes", "le fié 2 cervezas a don Pepe", "compré bolsas") → keep tipo/
  producto/cantidad, monto empty, requiere_confirmacion=SI. Never use a remembered or typical price.
- **Debt repayment**: "me pagó lo del aceite" → cobro_fiado with the debt's amount (11.00), producto of
  the debt, cantidad of the debt. Partial ("me pagó 10 de lo que debe") → producto `pago parcial <cliente>`,
  cantidad 1, monto stated.
- **Personal / non-business** ("para la casa, no es del negocio") → no row (`ignorar`).
- **Product naming**: lowercase, singular, Spanish accents fixed, typos fixed ("guevos"→"huevos",
  "azucar"→"azúcar", "atunes"→"atún"), keep brand when said ("leche gloria", "aceite primor"),
  supplier payments as "<product> proveedor" ("gaseosas proveedor"), refunds as "devolución <producto>",
  helper wages as "ayudante".
- Numbers may arrive as words or with typos ("tres cervezas a seis cincuenta" → 3 × 6.50 = 19.50).

## CSV output
Columns exactly `id_mensaje,tipo,producto,cantidad,monto,requiere_confirmacion`; monto with 2 decimals or
empty; cantidad integer when whole; SI/NO. Generated from the final DB state so corrections apply.

## Application service (`core/servicio.py`) — shared by every channel
`Libreta(memoria, *, interpretar=None)` (legacy positional `db_path` accepted and ignored for one release) owns follow-up answers, week dating, reports and batch:
`registrar(texto, *, tienda_id, id_mensaje=None, dia=None) -> Resultado` (an open pending question turns the
text into its answer), `registrar_audio(audio, nombre_archivo, *, tienda_id) -> (transcript, Resultado|None)`,
`transcribir(...)`, `pendiente(tienda_id)`, `omitir_pendiente(tienda_id, transaccion_id=None) -> bool`,
`transacciones(tienda_id, semana=None|"YYYY-MM-DD"|"all")`, `semanas(tienda_id)`, `flujo(tienda_id, semana=None)`,
`cuota(tienda_id)` (adds `semanas: [{semana, flujo}]`), `deudas(tienda_id)`, `reiniciar(tienda_id)`,
`lote(csv_bytes, *, prefijo) -> (csv_bytes, filas, flujo)`, `close()`.
Only Memoria touches databases, including week dates and the Telegram chat registry.
`Libreta.close()` closes its Memoria. SQLite alone migrates the old `telegram_semanas` table once.
All methods are blocking (database/OpenAI): async callers wrap them in `asyncio.to_thread`.
Channels must not reimplement this logic. Telegram: `crear_aplicacion(token, libreta) -> Application`.

## HTTP API (`canales/api.py`, FastAPI) — for the Vercel frontend
English paths and JSON keys; domain *values* stay Spanish (`type`: venta|gasto|fiado|cobro_fiado).
`create_app(libreta, *, api_key=None, cors_origins=()) -> FastAPI`.

Auth: every route except `GET /health` requires `Authorization: Bearer <API_KEY>` (env `API_KEY`; if unset the
API refuses to start unless `ALLOW_NO_AUTH=1`, for local dev). The Next.js app calls it from server routes, so the
key never reaches the browser. CORS origins from env `CORS_ORIGINS` (comma-separated).
`shop_id` is the tienda_id; `tg:<chat_id>` addresses the same notebook as that Telegram chat.

| Method & path | Body | Returns |
|---|---|---|
| `GET /health` | — | `{"status":"ok"}` |
| `POST /shops/{shop_id}/audio` | multipart `file` (webm/ogg/m4a/mp3/wav, ≤ 10 MB) | `{"transcript", "result": Result}`; 422 `{"detail":"no_speech"}` if nothing heard |
| `GET /shops/{shop_id}/pending` | — | `{"pending": {"transaction_id", "question"} \| null}` |
| `POST /shops/{shop_id}/pending/answer` | `{"text"}` | `Result`; 409 if nothing pending |
| `DELETE /shops/{shop_id}/pending` | — | `{"skipped": bool}` |
| `GET /shops/{shop_id}/transactions?week=YYYY-MM-DD\|all` | — | `{"week", "transactions": [Transaction]}` (default: current week) |
| `GET /shops/{shop_id}/debts` | — | `{"debts": [Debt]}` |
| `GET /shops/{shop_id}/cash-flow?week=` | — | `{"week","sales","collections","expenses","net_cash_flow","outstanding_credit","missing_amounts"}` |
| `GET /shops/{shop_id}/loan-estimate` | — | `{"avg_weekly_cash_flow","monthly_cash_flow","monthly_payment","percentage","explanation","weeks":[{"week","net_cash_flow"}]}` |
| `GET /shops/{shop_id}/summary` | — | `{"cash_flow", "loan_estimate", "debts", "pending", "transactions"}` (one call for the dashboard) |
| `POST /shops/{shop_id}/batch` | multipart `file` (.csv `id_mensaje,dia,mensaje`) | `text/csv` attachment `entrega.csv`; headers `X-Rows`, `X-Needs-Confirmation`, `X-Net-Cash-Flow` |
| `DELETE /shops/{shop_id}` | — | `{"reset": true}` |

Schemas: `Transaction {id, message_id, type, product, quantity, amount|null, needs_confirmation, day, customer, question}`;
`Debt {transaction_id, customer, product, original_amount|null, outstanding|null}`;
`Result {transactions:[Transaction], corrected:[Transaction], questions:[str], rejected:[str], ignored:[str], reply}`.

Runtime: one process. `main.py` runs uvicorn on `$PORT` (default 8000) serving the API, and — when
`TELEGRAM_TOKEN` is set — starts the Telegram bot's polling inside the same event loop (FastAPI lifespan),
sharing one `Libreta`. Railway: one service, public domain enabled, 1 replica for Telegram polling.

`abrir_memoria`: an explicit URL/path wins over `DATABASE_URL`, then `DB_PATH`, then `./libreta.db`.
`postgres://` and `postgresql://` select Postgres; other values select SQLite. Batch defaults explicitly
to `:memory:` regardless of production environment; `--db` opts into persistence. Evaluation stays in memory.

Postgres startup reads `db/schema.sql` (included in Docker): seven `public.libreta_*` tables,
`numeric(12,2)` amounts/quantities, identity bigint transaction/log IDs, timestamptz and jsonb.
Search uses a generated `simple` tsvector / GIN index plus ILIKE fallback without extensions.
Every table enables RLS with no policies, denying PostgREST anon access; the server uses the postgres
role, which bypasses RLS. Multi-statement writes commit or roll back together. This creates the schema;
it does not copy existing SQLite ledger rows to Postgres.

Opt-in integration tests: `TEST_DATABASE_URL=... .venv/bin/pytest tests/test_memoria_pg.py -q`.
Use a test database: tests apply the schema and use unique shop IDs, cleaning their rows afterwards.
