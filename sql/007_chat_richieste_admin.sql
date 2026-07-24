-- ============================================================================
-- 007 — Conversazioni con l'amministrazione (DA ESEGUIRE NELL'SQL EDITOR DI
-- SUPABASE)
--
-- Perché: la sezione "Contatta l'Amministrazione" nel profilo è una vera
-- chat bidirezionale (l'utente scrive, un admin risponde, si può continuare
-- a scambiarsi messaggi), non un modulo "invia e basta". Due tabelle:
-- chat_richieste è il contenitore/thread, messaggi_chat sono i singoli
-- messaggi al suo interno.
--
-- Scelte deliberate:
--  - Niente colonna per distinguere "messaggio utente" da "messaggio admin":
--    si ricava da id_autore confrontato con id_utente della chat (se
--    coincide è l'utente, altrimenti è un admin) — nessun dato duplicato.
--  - Niente flag "letto": non richiesto, un messaggio esiste o non esiste.
--  - I messaggi sono immutabili: nessuna colonna updated_at su messaggi_chat,
--    nessun endpoint di modifica previsto lato applicativo.
--  - chat_richieste.updated_at viene aggiornato dal backend ad ogni nuovo
--    messaggio (non da un trigger, per coerenza con il resto della base di
--    codice, che non usa trigger per questo tipo di logica): serve per
--    ordinare l'elenco chat lato Admin per "ultima attività".
--
-- Idempotente: create table/index "if not exists", si può rieseguire.
-- ============================================================================

create table if not exists public.chat_richieste (
  id bigint generated always as identity primary key,
  id_utente uuid not null references public.users(id) on delete cascade,
  id_sede uuid references public.sedi(id) on delete set null,
  oggetto text not null,
  -- 'aperta': in corso, in attesa di una risposta da una delle due parti.
  -- 'risolta': chiusa da un admin; un nuovo messaggio dell'utente la riapre
  -- automaticamente (gestito lato backend, non qui).
  stato text not null default 'aperta' check (stato in ('aperta', 'risolta')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.messaggi_chat (
  id bigint generated always as identity primary key,
  id_chat bigint not null references public.chat_richieste(id) on delete cascade,
  id_autore uuid not null references public.users(id),
  testo text not null,
  created_at timestamptz not null default now()
);

-- L'elenco chat lato Admin ordina per stato e ultima attività.
create index if not exists idx_chat_richieste_stato_updated on public.chat_richieste (stato, updated_at desc);

-- Il thread di una chat si carica sempre filtrando per id_chat e ordinando
-- per data: indice composito a supporto di entrambe le operazioni insieme.
create index if not exists idx_messaggi_chat_chat_created on public.messaggi_chat (id_chat, created_at);
