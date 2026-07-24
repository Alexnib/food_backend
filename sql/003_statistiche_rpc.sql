-- ============================================================================
-- 003 — Funzioni SQL per le statistiche (DA ESEGUIRE NELL'SQL EDITOR DI SUPABASE)
--
-- Perché: oggi gli endpoint statistiche scaricano TUTTE le righe di vendita in
-- Python e le sommano lì (tempo e traffico crescono linearmente con le vendite).
-- Con queste funzioni l'aggregazione avviene dentro Postgres: viaggia solo il
-- risultato già raggruppato (pochi KB), qualunque sia la dimensione della tabella.
--
-- Il backend le usa AUTOMATICAMENTE appena esistono (le rileva da solo, senza
-- riavvii): finché non vengono create continua a funzionare col percorso attuale.
-- Eseguire l'intero file così com'è. Ri-eseguirlo è innocuo (create or replace).
-- ============================================================================

-- Vendite aggregate per (giorno, prodotto): il mattone unico da cui derivano
-- overview, controllo di gestione, food cost analytics e andamento prodotti.
-- Replica ESATTAMENTE la logica di prezzo del backend: prima il prezzo salvato
-- sulla vendita (totale, poi unitario), infine il listino attuale come fallback;
-- lordo stimato col rapporto lordo/netto attuale del prodotto; food cost dal
-- costo ATTUALE (costo_ricetta_reale / prezzo_acquisto_netto).
create or replace function public.stat_vendite_aggregate(
  p_id_sede uuid,
  p_data_inizio date default null,
  p_data_fine_esclusiva date default null
)
returns table (
  giorno date,
  id_ricetta uuid,
  id_prodotto_commerciale uuid,
  quantita numeric,
  num_vendite bigint,
  ricavo_netto numeric,
  ricavo_lordo numeric,
  food_cost numeric
)
language sql
stable
as $$
  select
    v.data_vendita::date as giorno,
    v.id_ricetta,
    v.id_prodotto_commerciale,
    coalesce(sum(v.quantita), 0)::numeric as quantita,
    count(*)::bigint as num_vendite,
    coalesce(sum(
      coalesce(
        v.prezzo_totale,
        v.prezzo_singolo * v.quantita,
        coalesce(r.prezzo_vendita_netto, a.prezzo_vendita_netto, 0) * v.quantita
      )
    ), 0)::numeric as ricavo_netto,
    coalesce(sum(
      coalesce(
        v.prezzo_totale,
        v.prezzo_singolo * v.quantita,
        coalesce(r.prezzo_vendita_netto, a.prezzo_vendita_netto, 0) * v.quantita
      )
      * case
          when coalesce(r.prezzo_vendita_netto, a.prezzo_vendita_netto, 0) > 0
          then coalesce(r.prezzo_vendita_lordo, a.prezzo_vendita_lordo, 0)
               / coalesce(r.prezzo_vendita_netto, a.prezzo_vendita_netto)
          else 1
        end
    ), 0)::numeric as ricavo_lordo,
    coalesce(sum(
      v.quantita * coalesce(r.costo_ricetta_reale, a.prezzo_acquisto_netto, 0)
    ), 0)::numeric as food_cost
  from public.vendite v
  left join public.ricette r on r.id = v.id_ricetta
  left join public.articoli a on a.id = v.id_prodotto_commerciale
  where v.id_sede = p_id_sede
    and (p_data_inizio is null or v.data_vendita >= p_data_inizio)
    and (p_data_fine_esclusiva is null or v.data_vendita < p_data_fine_esclusiva)
  group by 1, 2, 3
  order by 1, 2, 3
$$;

-- Riepilogo mensile per il selettore mesi della pagina Vendite: conta e somma
-- per mese direttamente in SQL invece di scaricare ogni singola riga.
create or replace function public.stat_vendite_summary(
  p_id_sede uuid
)
returns table (
  mese text,
  numero_operazioni bigint,
  quantita_totale numeric
)
language sql
stable
as $$
  select
    to_char(v.data_vendita, 'YYYY-MM') as mese,
    count(*)::bigint as numero_operazioni,
    coalesce(sum(v.quantita), 0)::numeric as quantita_totale
  from public.vendite v
  where v.id_sede = p_id_sede
    and v.data_vendita is not null
  group by 1
  order by 1 desc
$$;

-- Le funzioni servono solo al backend (service_role): niente accesso anonimo.
revoke all on function public.stat_vendite_aggregate(uuid, date, date) from public, anon;
revoke all on function public.stat_vendite_summary(uuid) from public, anon;
grant execute on function public.stat_vendite_aggregate(uuid, date, date) to service_role;
grant execute on function public.stat_vendite_summary(uuid) to service_role;

-- Indice a supporto: quasi tutte le letture filtrano per sede e intervallo date.
create index if not exists idx_vendite_sede_data on public.vendite (id_sede, data_vendita);
