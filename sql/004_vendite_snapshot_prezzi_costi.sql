-- ============================================================================
-- 004 — Snapshot di prezzo lordo e food cost sulla vendita (DA ESEGUIRE
-- NELL'SQL EDITOR DI SUPABASE)
--
-- Perché: oggi "vendite" congela solo il prezzo netto (prezzo_singolo /
-- prezzo_totale) al momento della vendita. Il prezzo lordo e il food cost
-- vengono invece SEMPRE ricalcolati al volo dai valori ATTUALI di
-- ricette/articoli (aliquota IVA corrente, costo_ricetta_reale corrente,
-- prezzo_acquisto_netto corrente): se questi cambiano nel tempo, cambia
-- silenziosamente anche il bilancio di mesi già chiusi.
--
-- Con queste 4 colonne il backend può congelare lordo e food cost esattamente
-- come già fa per il netto, senza toccare in alcun modo ricette/articoli, che
-- restano liberamente modificabili in qualsiasi momento.
--
-- Volutamente NON generated/calcolate (prezzo_totale_lordo != prezzo_singolo_
-- lordo * quantita per costruzione in caso di sconti di riga, esattamente
-- come già oggi prezzo_totale può discostarsi da prezzo_singolo * quantita):
-- stessa logica già in uso per il netto, solo estesa a lordo e food cost.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS, si può rieseguire in sicurezza.
-- ============================================================================

alter table public.vendite
  add column if not exists prezzo_singolo_lordo double precision,
  add column if not exists prezzo_totale_lordo  double precision,
  add column if not exists food_cost_unitario    double precision,
  add column if not exists food_cost_totale      double precision;

comment on column public.vendite.prezzo_singolo_lordo is
  'Prezzo unitario lordo (IVA inclusa) applicato a questa vendita, congelato al momento dell''inserimento. NULL sulle vendite precedenti a questa colonna: in quel caso si ricade sul calcolo live (netto * aliquota IVA corrente).';

comment on column public.vendite.prezzo_totale_lordo is
  'Importo di riga lordo per questa vendita, congelato al momento dell''inserimento. Non derivato automaticamente da prezzo_singolo_lordo * quantita: può discostarsene in presenza di sconti di riga, come già avviene per prezzo_totale rispetto a prezzo_singolo * quantita.';

comment on column public.vendite.food_cost_unitario is
  'Costo (food cost) di una singola unità venduta, congelato al momento dell''inserimento: da ricette.costo_ricetta_reale se id_ricetta, altrimenti da articoli.prezzo_acquisto_netto se id_prodotto_commerciale. NULL sulle vendite precedenti a questa colonna: si ricade sul costo ATTUALE di ricetta/articolo.';

comment on column public.vendite.food_cost_totale is
  'Food cost di riga per questa vendita, congelato al momento dell''inserimento. Non derivato automaticamente da food_cost_unitario * quantita, per coerenza con prezzo_totale_lordo e per lasciare margine a correzioni manuali di riga.';
