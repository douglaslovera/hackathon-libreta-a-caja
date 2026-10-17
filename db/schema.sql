-- Applied atomically by MemoriaPostgres. No extensions or PostgREST policies needed.
CREATE TABLE IF NOT EXISTS public.libreta_transacciones (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tienda_id text NOT NULL, id_mensaje text NOT NULL, dia text, tipo text NOT NULL,
    producto text NOT NULL, cantidad numeric(12,2) NOT NULL, monto numeric(12,2),
    requiere_confirmacion boolean NOT NULL, cliente text, pregunta text,
    created_at timestamptz NOT NULL DEFAULT now(),
    busqueda tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, coalesce(producto, '') || ' ' || coalesce(cliente, ''))
    ) STORED
);
CREATE TABLE IF NOT EXISTS public.libreta_deudas (
    transaccion_id bigint PRIMARY KEY REFERENCES public.libreta_transacciones(id) ON DELETE CASCADE,
    tienda_id text NOT NULL, cliente text NOT NULL, producto text NOT NULL,
    monto_original numeric(12,2), pendiente numeric(12,2), cerrada boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS public.libreta_alias (
    tienda_id text NOT NULL, variante text NOT NULL, canonico text NOT NULL,
    PRIMARY KEY(tienda_id, variante)
);
CREATE TABLE IF NOT EXISTS public.libreta_pendientes (
    tienda_id text PRIMARY KEY, datos jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS public.libreta_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tienda_id text NOT NULL, id_mensaje text, texto text, salida_llm text, resultado jsonb,
    timestamp timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.libreta_semanas (
    tienda_id text NOT NULL, id_mensaje text NOT NULL, semana text NOT NULL,
    PRIMARY KEY(tienda_id, id_mensaje)
);
CREATE TABLE IF NOT EXISTS public.libreta_telegram_chats (chat_id bigint PRIMARY KEY);
CREATE INDEX IF NOT EXISTS libreta_transacciones_tienda_idx ON public.libreta_transacciones(tienda_id);
CREATE INDEX IF NOT EXISTS libreta_transacciones_busqueda_idx ON public.libreta_transacciones USING gin(busqueda);
CREATE INDEX IF NOT EXISTS libreta_deudas_tienda_idx ON public.libreta_deudas(tienda_id);
CREATE INDEX IF NOT EXISTS libreta_alias_tienda_idx ON public.libreta_alias(tienda_id);
CREATE INDEX IF NOT EXISTS libreta_pendientes_tienda_idx ON public.libreta_pendientes(tienda_id);
CREATE INDEX IF NOT EXISTS libreta_log_tienda_idx ON public.libreta_log(tienda_id);
CREATE INDEX IF NOT EXISTS libreta_semanas_tienda_idx ON public.libreta_semanas(tienda_id);
ALTER TABLE public.libreta_transacciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_deudas ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_alias ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_pendientes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_semanas ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.libreta_telegram_chats ENABLE ROW LEVEL SECURITY;
