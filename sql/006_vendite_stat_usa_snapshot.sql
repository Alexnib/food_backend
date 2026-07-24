-- ============================================================================
-- 006 — stat_vendite_aggregate usa lo snapshot congelato sulla vendita
-- (DA ESEGUIRE NELL'SQL EDITOR DI SUPABASE, DOPO 004 e 005)
--
-- Perché: finora ricavo_lordo e food_cost venivano SEMPRE ricalcolati dai
-- valori ATTUALI di ricette/articoli. Ora che "vendite" porta con sé
-- prezzo_singolo_lordo/prezzo_totale_lordo e food_cost_unitario/food_cost_
-- totale (congelati al momento della vendita, vedi 004+005), la funzione li
-- usa in priorità: il bilancio di un mese chiuso smette di muoversi quando
-- cambi una ricetta o un prezzo di acquisto oggi.
--
-- Il join su ricette/articoli resta SOLO come fallback per le vendite senza
-- snapshot (nessuna, dopo il backfill 005 — ma teoricamente possibile per
-- righe inserite da un percorso che non lo popola ancora).
--
-- Stessa firma di 003: create or replace non richiede di ri-concedere i
-- permessi (revoke/grant) già impostati lì. Idempotente, si può rieseguire.
-- ============================================================================

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
        v.prezzo_totale_lordo,
        v.prezzo_singolo_lordo * v.quantita,
        -- fallback: nessuno snapshot lordo su questa riga, stima dal rapporto
        -- lordo/netto ATTUALE del prodotto (comportamento pre-006).
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
      )
    ), 0)::numeric as ricavo_lordo,
    coalesce(sum(
      coalesce(
        v.food_cost_totale,
        v.food_cost_unitario * v.quantita,
        -- fallback: nessuno snapshot food cost su questa riga, costo ATTUALE
        -- di ricetta/articolo (comportamento pre-006).
        v.quantita * coalesce(r.costo_ricetta_reale, a.prezzo_acquisto_netto, 0)
      )
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
