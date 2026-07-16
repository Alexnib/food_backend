-- Da eseguire manualmente nell'SQL Editor di Supabase (nessun tool di migrazione automatico in questo repo).
--
-- Risolve il bug per cui /api/statistiche/overview e /api/statistiche/controllo-gestione/{anno}
-- scaricavano TUTTE le righe della tabella "vendite" per poi sommarle in Python. Su periodi con
-- molte vendite, la select senza .range()/.limit() veniva troncata dal cap di righe di default
-- di PostgREST/Supabase, facendo sparire dal grafico i giorni dopo il taglio (es. dopo il 17 del
-- mese) pur essendo le vendite presenti a DB.
--
-- Questa funzione fa l'aggregazione (SUM/GROUP BY) direttamente in Postgres: il backend riceve
-- già i totali per giorno/mese (poche decine di righe) invece di migliaia di righe di dettaglio,
-- eliminando il cap ed evitando di scalare linearmente con il numero di vendite.

-- Indice di supporto per il filtro id_sede + data_vendita usato da questa e da altre query su "vendite".
create index if not exists idx_vendite_sede_data on vendite (id_sede, data_vendita);

-- ATTENZIONE: verifica il tipo reale della colonna vendite.id_sede in Table Editor
-- (Database > Tables > vendite) prima di eseguire questo script. Se non è "uuid" ma
-- "integer"/"bigint", cambia il tipo del parametro p_id_sede qui sotto di conseguenza.
create or replace function stat_andamento_vendite(
  p_id_sede uuid,
  p_data_inizio date,
  p_data_fine_esclusiva date,   -- upper bound ESCLUSO (stessa convenzione già usata nel backend)
  p_group_by text default 'day' -- 'day' oppure 'month'
)
returns table (
  periodo text,
  ricavi numeric,
  food_cost numeric,
  numero_vendite bigint
)
language sql
stable
as $$
  select
    to_char(v.data_vendita, case when p_group_by = 'month' then 'YYYY-MM' else 'YYYY-MM-DD' end) as periodo,
    coalesce(sum(v.quantita * coalesce(r.prezzo_vendita_netto, a.prezzo_vendita_netto, 0)), 0)::numeric as ricavi,
    coalesce(sum(v.quantita * coalesce(r.costo_ricetta_reale, a.prezzo_acquisto_netto, 0)), 0)::numeric as food_cost,
    count(*)::bigint as numero_vendite
  from vendite v
  left join ricette  r on r.id = v.id_ricetta
  left join articoli a on a.id = v.id_prodotto_commerciale
  where v.id_sede = p_id_sede
    and v.data_vendita >= p_data_inizio
    and v.data_vendita <  p_data_fine_esclusiva
  group by periodo
  order by periodo;
$$;
