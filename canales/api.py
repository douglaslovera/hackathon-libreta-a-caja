"""HTTP transport for Libreta; domain values and explanations stay in Spanish."""
import asyncio
import secrets
from datetime import date
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Path, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core.servicio import semana_actual


class Transaction(BaseModel):
    """One recorded movement. Amounts are totals in Peruvian soles."""
    id: int | None
    message_id: str
    type: Literal['venta', 'gasto', 'fiado', 'cobro_fiado'] = Field(description='Spanish movement type', examples=['venta'])
    product: str = Field(examples=['pan'])
    quantity: float
    amount: float | None = Field(description='Total soles; null when unknown', examples=[3.0])
    needs_confirmation: bool
    day: str | None = Field(examples=['lunes'])
    customer: str | None
    question: str | None


class Debt(BaseModel):
    """An open fiado; unknown amounts remain null."""
    transaction_id: int
    customer: str
    product: str
    original_amount: float | None
    outstanding: float | None


class Result(BaseModel):
    """Recording outcome, including corrections and Spanish follow-up questions."""
    transactions: list[Transaction]
    corrected: list[Transaction]
    questions: list[str]
    rejected: list[str]
    ignored: list[str]
    reply: str = Field(description='Spanish reply ready to display')


class CashFlow(BaseModel):
    """Cash collected minus expenses; uncollected fiado is excluded."""
    week: str = Field(examples=['2026-10-12', 'all'])
    sales: float
    collections: float
    expenses: float
    net_cash_flow: float
    outstanding_credit: float
    missing_amounts: int


class WeekFlow(BaseModel):
    week: str
    net_cash_flow: float


class LoanEstimate(BaseModel):
    """Indicative monthly payment, never a credit approval."""
    avg_weekly_cash_flow: float
    monthly_cash_flow: float
    monthly_payment: float
    percentage: float = Field(examples=[0.3])
    explanation: str = Field(description='Spanish explanation and credit disclaimer')
    weeks: list[WeekFlow]


class Pending(BaseModel):
    """Question awaiting a response for a recorded transaction."""
    transaction_id: int
    question: str = Field(examples=['¿A cuánto vendiste el atún?'])


class Summary(BaseModel):
    """Dashboard snapshot for the current week, plus all open debts."""
    cash_flow: CashFlow
    loan_estimate: LoanEstimate
    debts: list[Debt]
    pending: Pending | None
    transactions: list[Transaction]


class AudioResult(BaseModel):
    transcript: str
    result: Result


class PendingResponse(BaseModel):
    pending: Pending | None


class TransactionsResponse(BaseModel):
    week: str
    transactions: list[Transaction]


class DebtsResponse(BaseModel):
    debts: list[Debt]


class Answer(BaseModel):
    text: str = Field(min_length=1, examples=['a 5.50 cada uno'])


class Skipped(BaseModel):
    skipped: bool


class Reset(BaseModel):
    reset: bool


def transaction(t) -> Transaction:
    return Transaction(id=t.id, message_id=t.id_mensaje, type=t.tipo, product=t.producto,
                       quantity=t.cantidad, amount=t.monto, needs_confirmation=t.requiere_confirmacion,
                       day=t.dia, customer=t.cliente, question=t.pregunta)


def debt(d) -> Debt:
    return Debt(transaction_id=d.transaccion_id, customer=d.cliente, product=d.producto,
                original_amount=d.monto_original, outstanding=d.pendiente)


def result(r) -> Result:
    return Result(transactions=[transaction(t) for t in r.transacciones],
                  corrected=[transaction(t) for t in r.corregidas], questions=r.preguntas,
                  rejected=r.rechazos, ignored=r.ignorados, reply=r.respuesta)


def pending(p) -> Pending | None:
    if p is None:
        return None
    return Pending(transaction_id=p.get('transaccion_id', p.get('id', p.get('ref_transaccion'))),
                   question=p.get('pregunta', '¿Cuál es el monto?'))


def cash_flow(f) -> CashFlow:
    return CashFlow(week=f['semana'], sales=f['ventas'], collections=f['cobros'], expenses=f['gastos'],
                    net_cash_flow=f['flujo'], outstanding_credit=f['fiado_pendiente'],
                    missing_amounts=f['sin_monto'])


def loan_estimate(c) -> LoanEstimate:
    return LoanEstimate(avg_weekly_cash_flow=c['flujo_semanal_promedio'], monthly_cash_flow=c['flujo_mensual'],
                        monthly_payment=c['cuota'], percentage=c['porcentaje'], explanation=c['explicacion'],
                        weeks=[WeekFlow(week=s['semana'], net_cash_flow=s['flujo']) for s in c['semanas']])


ShopId = Annotated[str, Path(pattern=r'^[A-Za-z0-9:_-]{1,64}$', description='Shop identifier, e.g. tg:1234')]


def validated_week(week: Annotated[str | None, Query(description='YYYY-MM-DD or all; defaults to this week')] = None):
    if week is not None and week != 'all':
        try:
            if date.fromisoformat(week).isoformat() != week:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail='week must be YYYY-MM-DD or all')
    return week


AUDIO_TYPES = {
    'audio/webm': '.webm', 'video/webm': '.webm', 'audio/ogg': '.ogg', 'application/ogg': '.ogg',
    'audio/mp4': '.m4a', 'audio/m4a': '.m4a', 'audio/x-m4a': '.m4a',
    'audio/mpeg': '.mp3', 'audio/mp3': '.mp3', 'audio/wav': '.wav',
    'audio/x-wav': '.wav', 'audio/wave': '.wav', 'audio/vnd.wave': '.wav',
}
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def create_app(libreta, *, api_key=None, cors_origins=()) -> FastAPI:
    app = FastAPI(title='Libreta → Caja API', description='Shop notebook; monetary amounts in PEN.',
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins),
                       allow_methods=['GET', 'POST', 'DELETE'], allow_headers=['Authorization', 'Content-Type'],
                       expose_headers=['X-Rows', 'X-Needs-Confirmation', 'X-Net-Cash-Flow', 'Content-Disposition'])
    bearer = HTTPBearer(auto_error=False)

    async def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if api_key is None:
            return
        if (credentials is None or credentials.scheme.lower() != 'bearer'
                or not secrets.compare_digest(credentials.credentials.encode(), api_key.encode())):
            raise HTTPException(status_code=401, detail='Unauthorized', headers={'WWW-Authenticate': 'Bearer'})

    router = APIRouter(dependencies=[Depends(authorize)])

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @router.get('/docs', include_in_schema=False)
    async def docs():
        return get_swagger_ui_html(openapi_url='/openapi.json', title=app.title)

    @router.get('/redoc', include_in_schema=False)
    async def redoc():
        return get_redoc_html(openapi_url='/openapi.json', title=app.title)

    @router.get('/openapi.json', include_in_schema=False)
    async def schema():
        return app.openapi()

    @router.post('/shops/{shop_id}/audio', response_model=AudioResult)
    async def audio(shop_id: ShopId, file: Annotated[UploadFile, File(description='Voice note, at most 10 MB')]):
        name = file.filename
        suffix = FilePath(name).suffix.lower() if name else ''
        content_type = (file.content_type or '').split(';', 1)[0].strip().lower()
        if ((suffix and suffix not in set(AUDIO_TYPES.values()))
                or content_type not in {*AUDIO_TYPES, 'application/octet-stream', ''}
                or (not suffix and content_type not in AUDIO_TYPES)):
            raise HTTPException(status_code=415, detail='unsupported_audio_type')
        name = name if suffix else f'nota{AUDIO_TYPES[content_type]}'
        data = await file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail='audio_too_large')
        transcript, recorded = await asyncio.to_thread(libreta.registrar_audio, data, name, tienda_id=shop_id)
        if (transcript, recorded) == ('', None):
            raise HTTPException(status_code=422, detail='no_speech')
        return AudioResult(transcript=transcript, result=result(recorded))

    @router.get('/shops/{shop_id}/pending', response_model=PendingResponse)
    async def get_pending(shop_id: ShopId):
        return PendingResponse(pending=pending(await asyncio.to_thread(libreta.pendiente, shop_id)))

    @router.post('/shops/{shop_id}/pending/answer', response_model=Result)
    async def answer(shop_id: ShopId, body: Answer):
        if await asyncio.to_thread(libreta.pendiente, shop_id) is None:
            raise HTTPException(status_code=409, detail='no_pending_question')
        return result(await asyncio.to_thread(libreta.registrar, body.text, tienda_id=shop_id))

    @router.delete('/shops/{shop_id}/pending', response_model=Skipped)
    async def skip_pending(shop_id: ShopId):
        return Skipped(skipped=await asyncio.to_thread(libreta.omitir_pendiente, shop_id))

    @router.get('/shops/{shop_id}/transactions', response_model=TransactionsResponse)
    async def transactions(shop_id: ShopId, week: Annotated[str | None, Depends(validated_week)]):
        rows = await asyncio.to_thread(libreta.transacciones, shop_id, week)
        return TransactionsResponse(week=week or semana_actual(), transactions=[transaction(t) for t in rows])

    @router.get('/shops/{shop_id}/debts', response_model=DebtsResponse)
    async def debts(shop_id: ShopId):
        return DebtsResponse(debts=[debt(d) for d in await asyncio.to_thread(libreta.deudas, shop_id)])

    @router.get('/shops/{shop_id}/cash-flow', response_model=CashFlow)
    async def flow(shop_id: ShopId, week: Annotated[str | None, Depends(validated_week)]):
        return cash_flow(await asyncio.to_thread(libreta.flujo, shop_id, week))

    @router.get('/shops/{shop_id}/loan-estimate', response_model=LoanEstimate)
    async def estimate(shop_id: ShopId):
        return loan_estimate(await asyncio.to_thread(libreta.cuota, shop_id))

    @router.get('/shops/{shop_id}/summary', response_model=Summary)
    async def summary(shop_id: ShopId):
        # Sequential calls preserve the service's SQLite access pattern.
        return Summary(cash_flow=cash_flow(await asyncio.to_thread(libreta.flujo, shop_id)),
                       loan_estimate=loan_estimate(await asyncio.to_thread(libreta.cuota, shop_id)),
                       debts=[debt(d) for d in await asyncio.to_thread(libreta.deudas, shop_id)],
                       pending=pending(await asyncio.to_thread(libreta.pendiente, shop_id)),
                       transactions=[transaction(t) for t in await asyncio.to_thread(libreta.transacciones, shop_id)])

    @router.post('/shops/{shop_id}/batch', response_class=Response,
                 responses={200: {'content': {'text/csv': {}}, 'description': 'entrega.csv attachment'}})
    async def batch(shop_id: ShopId, file: Annotated[UploadFile, File(description='CSV: id_mensaje,dia,mensaje')]):
        if not file.filename or FilePath(file.filename).suffix.lower() != '.csv':
            raise HTTPException(status_code=415, detail='expected_csv')
        content, rows, flow = await asyncio.to_thread(libreta.lote, await file.read(), prefijo=shop_id)
        return Response(content=content, media_type='text/csv', headers={
            'Content-Disposition': 'attachment; filename="entrega.csv"',
            'X-Rows': str(len(rows)), 'X-Needs-Confirmation': str(sum(t.requiere_confirmacion for t in rows)),
            'X-Net-Cash-Flow': str(flow['flujo']),
        })

    @router.delete('/shops/{shop_id}', response_model=Reset)
    async def reset(shop_id: ShopId):
        await asyncio.to_thread(libreta.reiniciar, shop_id)
        return Reset(reset=True)

    app.include_router(router)
    return app
