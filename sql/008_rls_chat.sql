-- ============================================================================
-- 008 — RLS su chat_richieste e messaggi_chat (DA ESEGUIRE NELL'SQL EDITOR DI
-- SUPABASE, DOPO 007)
--
-- Il backend parla con queste tabelle usando la service_role key
-- (database/config.py), che ignora SEMPRE la RLS per definizione: questa
-- riga non cambia nulla per l'app così com'è oggi, serve solo a soddisfare
-- il security linter di Supabase (avvisa se una tabella in "public" non ha
-- la RLS attiva).
--
-- Nessuna policy definita di proposito: RLS attiva + zero policy significa
-- accesso negato di default per qualunque chiave diversa dalla service_role
-- (anon/authenticated) — la configurazione più semplice e già corretta,
-- dato che nell'app nessuno tocca queste tabelle se non il backend.
--
-- Se in futuro il frontend dovesse mai parlare direttamente con Supabase per
-- queste tabelle (es. Supabase Realtime per i messaggi invece del polling),
-- a quel punto servirebbero policy vere e proprie — non prima.
--
-- Idempotente: ENABLE ROW LEVEL SECURITY è sicuro da rieseguire.
-- ============================================================================

alter table public.chat_richieste enable row level security;
alter table public.messaggi_chat enable row level security;
