"""Postgres ledger. Each operation borrows a connection and commits atomically."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import re

import psycopg.conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core.modelos import Contexto, Deuda, Resultado, Transaccion


class MemoriaPostgres:
    def __init__(self, dsn: str):
        # Validate the DSN up front: a bad string makes the pool retry silently for ~30s.
        psycopg.conninfo.conninfo_to_dict(dsn)
        self._pool = ConnectionPool(dsn, min_size=1, max_size=5, timeout=15, open=True,
                                    kwargs={"prepare_threshold": None, "row_factory": dict_row,
                                            "connect_timeout": 10})
        try:
            self._pool.wait(timeout=15)
            with self._pool.connection() as db:
                db.execute((Path(__file__).resolve().parents[1] / 'db/schema.sql').read_text(), prepare=False)
        except BaseException:
            self._pool.close()
            raise

    @staticmethod
    def _numeros(row):
        return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}

    @classmethod
    def _transaccion(cls, row):
        if row is None:
            return None
        data = cls._numeros(row)
        data.pop('busqueda', None)
        created_at = data.pop('created_at').isoformat()
        t = Transaccion(**data)
        t.created_at = created_at
        return t

    @staticmethod
    def _crear_deuda(db, t):
        db.execute('INSERT INTO public.libreta_deudas (transaccion_id,tienda_id,cliente,producto,monto_original,pendiente) VALUES (%s,%s,%s,%s,%s,%s)',
                   (t.id, t.tienda_id, t.cliente or '', t.producto, t.monto, t.monto))

    def agregar(self, t: Transaccion) -> Transaccion:
        data = asdict(t)
        data.pop('id')
        with self._pool.connection() as db:
            row = db.execute(f"INSERT INTO public.libreta_transacciones ({','.join(data)}) VALUES ({','.join('%s' for _ in data)}) RETURNING *", tuple(data.values())).fetchone()
            stored = self._transaccion(row)
            if stored.tipo == 'fiado':
                self._crear_deuda(db, stored)
        t.id, t.created_at = stored.id, stored.created_at
        return t

    def actualizar(self, transaccion_id: int, **campos) -> Transaccion:
        allowed = set(Transaccion.__dataclass_fields__) - {'id', 'tienda_id', 'id_mensaje'}
        if not campos or not set(campos) <= allowed:
            raise ValueError('Campos de actualización inválidos')
        with self._pool.connection() as db:
            old = db.execute('SELECT id FROM public.libreta_transacciones WHERE id=%s FOR UPDATE', (transaccion_id,)).fetchone()
            if old is None:
                raise ValueError('Transacción inexistente')
            row = db.execute(f"UPDATE public.libreta_transacciones SET {','.join(f'{k}=%s' for k in campos)} WHERE id=%s RETURNING *", (*campos.values(), transaccion_id)).fetchone()
            t = self._transaccion(row)
            debt = db.execute('SELECT * FROM public.libreta_deudas WHERE transaccion_id=%s FOR UPDATE', (t.id,)).fetchone()
            if t.tipo != 'fiado':
                db.execute('DELETE FROM public.libreta_deudas WHERE transaccion_id=%s', (t.id,))
            elif debt is None:
                self._crear_deuda(db, t)
            else:
                debt = self._numeros(debt)
                pending, closed = debt['pendiente'], debt['cerrada']
                if 'monto' in campos:
                    paid = ((debt['monto_original'] or 0) - (pending or 0)) if debt['monto_original'] is not None else 0
                    pending = None if t.monto is None else round(max(0, t.monto - paid), 2)
                    closed = pending == 0 if pending is not None else closed
                db.execute('UPDATE public.libreta_deudas SET cliente=%s,producto=%s,monto_original=%s,pendiente=%s,cerrada=%s WHERE transaccion_id=%s',
                           (t.cliente or '', t.producto, t.monto, pending, closed, t.id))
            return t

    def obtener(self, transaccion_id: int) -> Transaccion | None:
        with self._pool.connection() as db:
            return self._transaccion(db.execute('SELECT * FROM public.libreta_transacciones WHERE id=%s', (transaccion_id,)).fetchone())

    def transacciones(self, tienda_id: str) -> list[Transaccion]:
        with self._pool.connection() as db:
            return [self._transaccion(r) for r in db.execute('SELECT * FROM public.libreta_transacciones WHERE tienda_id=%s ORDER BY id', (tienda_id,))]

    def ultimas(self, tienda_id: str, n: int = 10) -> list[Transaccion]:
        with self._pool.connection() as db:
            rows = db.execute('SELECT * FROM public.libreta_transacciones WHERE tienda_id=%s ORDER BY id DESC LIMIT %s', (tienda_id, max(0, n))).fetchall()
            return [self._transaccion(r) for r in reversed(rows)]

    def deudas_abiertas(self, tienda_id: str) -> list[Deuda]:
        with self._pool.connection() as db:
            rows = db.execute('SELECT transaccion_id,cliente,producto,monto_original,pendiente FROM public.libreta_deudas WHERE tienda_id=%s AND NOT cerrada ORDER BY transaccion_id', (tienda_id,))
            return [Deuda(**self._numeros(r)) for r in rows]

    def registrar_cobro(self, tienda_id: str, deuda_transaccion_id: int, monto: float | None) -> None:
        if monto is not None and monto < 0:
            raise ValueError('Un cobro no puede ser negativo')
        with self._pool.connection() as db:
            # Same lock order as actualizar: transaction first, then its debt.
            db.execute('SELECT id FROM public.libreta_transacciones WHERE tienda_id=%s AND id=%s FOR UPDATE', (tienda_id, deuda_transaccion_id)).fetchone()
            debt = db.execute('SELECT * FROM public.libreta_deudas WHERE tienda_id=%s AND transaccion_id=%s AND NOT cerrada FOR UPDATE', (tienda_id, deuda_transaccion_id)).fetchone()
            if debt is None:
                raise ValueError('Deuda abierta inexistente para esta tienda')
            debt = self._numeros(debt)
            pending = 0 if monto is None else (None if debt['pendiente'] is None else round(max(0, debt['pendiente'] - monto), 2))
            db.execute('UPDATE public.libreta_deudas SET pendiente=%s,cerrada=%s WHERE transaccion_id=%s', (pending, pending == 0, deuda_transaccion_id))

    def buscar(self, tienda_id: str, texto: str, n: int = 5) -> list[Transaccion]:
        words = re.findall(r'\w+', texto, re.UNICODE)
        if not words or n <= 0:
            return []
        query = ' | '.join("'" + w + "':*" for w in words)
        # translate handles Spanish accents without requiring the unaccent extension.
        folded = "translate(lower(coalesce(producto,'') || ' ' || coalesce(cliente,'')), 'áéíóúüñ', 'aeiouun')"
        conditions = ' OR '.join(f"{folded} ILIKE translate(lower(%s), 'áéíóúüñ', 'aeiouun') ESCAPE '!'" for _ in words)
        patterns = ['%' + w.replace('!', '!!').replace('_', '!_') + '%' for w in words]
        with self._pool.connection() as db:
            rows = db.execute(f'SELECT * FROM public.libreta_transacciones WHERE tienda_id=%s AND (busqueda @@ to_tsquery(\'simple\', %s) OR {conditions}) ORDER BY ts_rank(busqueda,to_tsquery(\'simple\',%s)) DESC,id DESC LIMIT %s',
                              (tienda_id, query, *patterns, query, n)).fetchall()
            return [self._transaccion(r) for r in rows]

    def alias(self, tienda_id: str) -> dict[str, str]:
        with self._pool.connection() as db:
            return {r['variante']: r['canonico'] for r in db.execute('SELECT variante,canonico FROM public.libreta_alias WHERE tienda_id=%s', (tienda_id,))}

    def guardar_alias(self, tienda_id: str, variante: str, canonico: str) -> None:
        with self._pool.connection() as db:
            db.execute('INSERT INTO public.libreta_alias VALUES (%s,%s,%s) ON CONFLICT (tienda_id,variante) DO UPDATE SET canonico=EXCLUDED.canonico', (tienda_id, variante.strip().lower(), canonico.strip().lower()))

    def pendiente(self, tienda_id: str) -> dict | None:
        with self._pool.connection() as db:
            row = db.execute('SELECT datos FROM public.libreta_pendientes WHERE tienda_id=%s', (tienda_id,)).fetchone()
            return row['datos'] if row else None

    def fijar_pendiente(self, tienda_id: str, datos: dict | None) -> None:
        with self._pool.connection() as db:
            if datos is None:
                db.execute('DELETE FROM public.libreta_pendientes WHERE tienda_id=%s', (tienda_id,))
            else:
                db.execute('INSERT INTO public.libreta_pendientes VALUES (%s,%s) ON CONFLICT (tienda_id) DO UPDATE SET datos=EXCLUDED.datos', (tienda_id, Jsonb(datos)))

    def log(self, tienda_id: str, id_mensaje: str, texto: str, salida_llm: str, resultado: Resultado) -> None:
        with self._pool.connection() as db:
            db.execute('INSERT INTO public.libreta_log (tienda_id,id_mensaje,texto,salida_llm,resultado) VALUES (%s,%s,%s,%s,%s)', (tienda_id, id_mensaje, texto, salida_llm, Jsonb(asdict(resultado))))

    def borrar_tienda(self, tienda_id: str) -> None:
        with self._pool.connection() as db:
            for table in ('transacciones', 'deudas', 'alias', 'pendientes', 'log', 'semanas'):
                db.execute(f'DELETE FROM public.libreta_{table} WHERE tienda_id=%s', (tienda_id,))

    def contexto(self, tienda_id: str, dia: str | None = None) -> Contexto:
        return Contexto(dia=dia, deudas_abiertas=self.deudas_abiertas(tienda_id), ultimas=self.ultimas(tienda_id), alias=self.alias(tienda_id))

    def fechar_semana(self, tienda_id: str, id_mensaje: str, semana: str) -> None:
        with self._pool.connection() as db:
            db.execute('INSERT INTO public.libreta_semanas VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (tienda_id, id_mensaje, semana))

    def semanas_mensaje(self, tienda_id: str) -> dict[str, str]:
        with self._pool.connection() as db:
            return {r['id_mensaje']: r['semana'] for r in db.execute('SELECT id_mensaje,semana FROM public.libreta_semanas WHERE tienda_id=%s', (tienda_id,))}

    def recordar_chat(self, chat_id: int) -> None:
        with self._pool.connection() as db:
            db.execute('INSERT INTO public.libreta_telegram_chats VALUES (%s) ON CONFLICT DO NOTHING', (chat_id,))

    def chats(self) -> list[int]:
        with self._pool.connection() as db:
            return [r['chat_id'] for r in db.execute('SELECT chat_id FROM public.libreta_telegram_chats ORDER BY chat_id')]

    def close(self) -> None:
        self._pool.close()
