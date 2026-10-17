"""Thread-safe SQLite ledger. All public operations serialize on one connection."""
from dataclasses import asdict
import json
import os
import re
import sqlite3
import threading

from core.modelos import Contexto, Deuda, Resultado, Transaccion


class MemoriaSQLite:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS transacciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tienda_id TEXT NOT NULL,
                id_mensaje TEXT NOT NULL, dia TEXT, tipo TEXT NOT NULL,
                producto TEXT NOT NULL, cantidad REAL NOT NULL, monto REAL,
                requiere_confirmacion INTEGER NOT NULL, cliente TEXT, pregunta TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')));
            CREATE TABLE IF NOT EXISTS deudas (
                transaccion_id INTEGER PRIMARY KEY, cliente TEXT NOT NULL,
                producto TEXT NOT NULL, monto_original REAL, pendiente REAL,
                cerrada INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS alias (
                tienda_id TEXT, variante TEXT, canonico TEXT,
                PRIMARY KEY(tienda_id, variante));
            CREATE TABLE IF NOT EXISTS pendientes (tienda_id TEXT PRIMARY KEY, datos TEXT);
            CREATE TABLE IF NOT EXISTS log (
                id INTEGER PRIMARY KEY, tienda_id TEXT, id_mensaje TEXT,
                texto TEXT, salida_llm TEXT, resultado TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS semanas_mensaje (
                tienda_id TEXT, id_mensaje TEXT, semana TEXT NOT NULL,
                PRIMARY KEY(tienda_id, id_mensaje));
            CREATE TABLE IF NOT EXISTS telegram_chats (chat_id INTEGER PRIMARY KEY);
        """)
        # Carry the old bot's dates over once, then remove the obsolete source.
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_semanas'").fetchone():
            self._db.execute("INSERT OR IGNORE INTO semanas_mensaje SELECT tienda_id,id_mensaje,semana FROM telegram_semanas")
            self._db.execute("DROP TABLE telegram_semanas")
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS transacciones_fts USING fts5(producto, cliente, tokenize='unicode61 remove_diacritics 2')")
            # Rebuild on opening, including ledgers created without FTS support.
            self._db.execute("DELETE FROM transacciones_fts")
            self._db.execute("INSERT INTO transacciones_fts(rowid,producto,cliente) SELECT id,producto,cliente FROM transacciones")
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        self._db.commit()

    @staticmethod
    def _transaccion(row):
        if row is None:
            return None
        data = dict(row)
        created_at = data.pop('created_at', None)
        data['requiere_confirmacion'] = bool(data['requiere_confirmacion'])
        t = Transaccion(**data)
        # Optional metadata without changing the frozen CSV/dataclass contract.
        t.created_at = created_at
        return t

    def _indexar(self, t):
        if self._fts:
            self._db.execute('DELETE FROM transacciones_fts WHERE rowid=?', (t.id,))
            self._db.execute('INSERT INTO transacciones_fts(rowid,producto,cliente) VALUES (?,?,?)', (t.id, t.producto, t.cliente))

    def agregar(self, t: Transaccion) -> Transaccion:
        with self._lock, self._db:
            data = asdict(t)
            data.pop('id')
            columns = ','.join(data)
            cursor = self._db.execute(f"INSERT INTO transacciones ({columns}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
            t.id = cursor.lastrowid
            t.created_at = self._db.execute('SELECT created_at FROM transacciones WHERE id=?', (t.id,)).fetchone()[0]
            if t.tipo == 'fiado':
                self._db.execute('INSERT INTO deudas VALUES (?,?,?,?,?,0)', (t.id, t.cliente or '', t.producto, t.monto, t.monto))
            self._indexar(t)
        return t

    def actualizar(self, transaccion_id: int, **campos) -> Transaccion:
        allowed = set(Transaccion.__dataclass_fields__) - {'id', 'tienda_id', 'id_mensaje'}
        if not campos or not set(campos) <= allowed:
            raise ValueError('Campos de actualización inválidos')
        with self._lock, self._db:
            old = self._transaccion(self._db.execute('SELECT * FROM transacciones WHERE id=?', (transaccion_id,)).fetchone())
            if old is None:
                raise ValueError('Transacción inexistente')
            self._db.execute(f"UPDATE transacciones SET {','.join(f'{k}=?' for k in campos)} WHERE id=?", (*campos.values(), transaccion_id))
            t = self._transaccion(self._db.execute('SELECT * FROM transacciones WHERE id=?', (transaccion_id,)).fetchone())
            debt = self._db.execute('SELECT * FROM deudas WHERE transaccion_id=?', (t.id,)).fetchone()
            if t.tipo != 'fiado':
                self._db.execute('DELETE FROM deudas WHERE transaccion_id=?', (t.id,))
            elif debt is None:
                self._db.execute('INSERT INTO deudas VALUES (?,?,?,?,?,0)', (t.id, t.cliente or '', t.producto, t.monto, t.monto))
            else:
                pending, closed = debt['pendiente'], debt['cerrada']
                if 'monto' in campos:
                    paid = ((debt['monto_original'] or 0) - (pending or 0)) if debt['monto_original'] is not None else 0
                    pending = None if t.monto is None else round(max(0, t.monto - paid), 2)
                    closed = int(pending == 0) if pending is not None else closed
                self._db.execute('UPDATE deudas SET cliente=?,producto=?,monto_original=?,pendiente=?,cerrada=? WHERE transaccion_id=?', (t.cliente or '', t.producto, t.monto, pending, closed, t.id))
            self._indexar(t)
            return t

    def obtener(self, transaccion_id: int) -> Transaccion | None:
        with self._lock:
            return self._transaccion(self._db.execute('SELECT * FROM transacciones WHERE id=?', (transaccion_id,)).fetchone())

    def transacciones(self, tienda_id: str) -> list[Transaccion]:
        with self._lock:
            return [self._transaccion(r) for r in self._db.execute('SELECT * FROM transacciones WHERE tienda_id=? ORDER BY id', (tienda_id,))]

    def ultimas(self, tienda_id: str, n: int = 10) -> list[Transaccion]:
        with self._lock:
            rows = self._db.execute('SELECT * FROM transacciones WHERE tienda_id=? ORDER BY id DESC LIMIT ?', (tienda_id, max(0, n))).fetchall()
            return [self._transaccion(r) for r in reversed(rows)]

    def deudas_abiertas(self, tienda_id: str) -> list[Deuda]:
        with self._lock:
            rows = self._db.execute('SELECT d.transaccion_id,d.cliente,d.producto,d.monto_original,d.pendiente FROM deudas d JOIN transacciones t ON t.id=d.transaccion_id WHERE t.tienda_id=? AND d.cerrada=0 ORDER BY t.id', (tienda_id,))
            return [Deuda(**dict(r)) for r in rows]

    def registrar_cobro(self, tienda_id: str, deuda_transaccion_id: int, monto: float | None) -> None:
        if monto is not None and monto < 0:
            raise ValueError('Un cobro no puede ser negativo')
        with self._lock, self._db:
            debt = self._db.execute('SELECT d.* FROM deudas d JOIN transacciones t ON t.id=d.transaccion_id WHERE t.tienda_id=? AND t.id=? AND d.cerrada=0', (tienda_id, deuda_transaccion_id)).fetchone()
            if debt is None:
                raise ValueError('Deuda abierta inexistente para esta tienda')
            pending = 0 if monto is None else (None if debt['pendiente'] is None else round(max(0, debt['pendiente'] - monto), 2))
            self._db.execute('UPDATE deudas SET pendiente=?,cerrada=? WHERE transaccion_id=?', (pending, int(pending == 0), deuda_transaccion_id))

    def buscar(self, tienda_id: str, texto: str, n: int = 5) -> list[Transaccion]:
        words = re.findall(r'\w+', texto, re.UNICODE)
        if not words or n <= 0:
            return []
        with self._lock:
            if self._fts:
                query = ' OR '.join('"' + w + '"*' for w in words)
                rows = self._db.execute('SELECT t.* FROM transacciones t JOIN transacciones_fts f ON t.id=f.rowid WHERE t.tienda_id=? AND transacciones_fts MATCH ? ORDER BY rank,t.id DESC LIMIT ?', (tienda_id, query, n)).fetchall()
            else:
                conditions = ' OR '.join('(producto LIKE ? OR cliente LIKE ?)' for _ in words)
                args = [f'%{w}%' for w in words for _ in range(2)]
                rows = self._db.execute(f'SELECT * FROM transacciones WHERE tienda_id=? AND ({conditions}) ORDER BY id DESC LIMIT ?', (tienda_id, *args, n)).fetchall()
            return [self._transaccion(r) for r in rows]

    def alias(self, tienda_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._db.execute('SELECT variante,canonico FROM alias WHERE tienda_id=?', (tienda_id,)).fetchall())

    def guardar_alias(self, tienda_id: str, variante: str, canonico: str) -> None:
        with self._lock, self._db:
            self._db.execute('INSERT OR REPLACE INTO alias VALUES (?,?,?)', (tienda_id, variante.strip().lower(), canonico.strip().lower()))

    def pendiente(self, tienda_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute('SELECT datos FROM pendientes WHERE tienda_id=?', (tienda_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def fijar_pendiente(self, tienda_id: str, datos: dict | None) -> None:
        with self._lock, self._db:
            if datos is None:
                self._db.execute('DELETE FROM pendientes WHERE tienda_id=?', (tienda_id,))
            else:
                self._db.execute('INSERT OR REPLACE INTO pendientes VALUES (?,?)', (tienda_id, json.dumps(datos, ensure_ascii=False)))

    def log(self, tienda_id: str, id_mensaje: str, texto: str, salida_llm: str, resultado: Resultado) -> None:
        with self._lock, self._db:
            self._db.execute('INSERT INTO log(tienda_id,id_mensaje,texto,salida_llm,resultado) VALUES (?,?,?,?,?)', (tienda_id, id_mensaje, texto, salida_llm, json.dumps(asdict(resultado), ensure_ascii=False)))

    def borrar_tienda(self, tienda_id: str) -> None:
        with self._lock, self._db:
            if self._fts:
                self._db.execute('DELETE FROM transacciones_fts WHERE rowid IN (SELECT id FROM transacciones WHERE tienda_id=?)', (tienda_id,))
            self._db.execute('DELETE FROM deudas WHERE transaccion_id IN (SELECT id FROM transacciones WHERE tienda_id=?)', (tienda_id,))
            for table in ('transacciones', 'alias', 'pendientes', 'log', 'semanas_mensaje'):
                self._db.execute(f'DELETE FROM {table} WHERE tienda_id=?', (tienda_id,))

    def contexto(self, tienda_id: str, dia: str | None = None) -> Contexto:
        return Contexto(dia=dia, deudas_abiertas=self.deudas_abiertas(tienda_id), ultimas=self.ultimas(tienda_id), alias=self.alias(tienda_id))

    def fechar_semana(self, tienda_id: str, id_mensaje: str, semana: str) -> None:
        with self._lock, self._db:
            self._db.execute('INSERT OR IGNORE INTO semanas_mensaje VALUES (?,?,?)', (tienda_id, id_mensaje, semana))

    def semanas_mensaje(self, tienda_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._db.execute('SELECT id_mensaje,semana FROM semanas_mensaje WHERE tienda_id=?', (tienda_id,)))

    def recordar_chat(self, chat_id: int) -> None:
        with self._lock, self._db:
            self._db.execute('INSERT OR IGNORE INTO telegram_chats VALUES (?)', (chat_id,))

    def chats(self) -> list[int]:
        with self._lock:
            return [r[0] for r in self._db.execute('SELECT chat_id FROM telegram_chats ORDER BY chat_id')]

    def close(self) -> None:
        with self._lock:
            self._db.close()


Memoria = MemoriaSQLite


def abrir_memoria(url: str | None = None):
    destino = url or os.getenv('DATABASE_URL') or os.getenv('DB_PATH') or './libreta.db'
    if destino.startswith(('postgres://', 'postgresql://')):
        from core.memoria_pg import MemoriaPostgres
        return MemoriaPostgres(destino)
    if '://' in destino:
        # Never fall back to a SQLite file named after a URL: data would silently vanish on redeploy.
        raise ValueError(
            'DATABASE_URL debe ser una cadena de conexión Postgres (postgresql://...), p. ej. el '
            '"Session pooler" de Supabase; la URL https://<ref>.supabase.co es la de la API REST.')
    return MemoriaSQLite(destino)
