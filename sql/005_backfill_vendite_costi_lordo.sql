-- ============================================================================
-- 005 — Backfill di food_cost e prezzo lordo sulle vendite già registrate
-- (DA ESEGUIRE NELL'SQL EDITOR DI SUPABASE, DOPO 004)
--
-- Perché: le 4 colonne aggiunte in 004 sono NULL su tutte le vendite già
-- registrate. In questo caso i costi (costo_ricetta_reale / prezzo_acquisto_
-- netto) e i prezzi di listino non sono mai cambiati nel tempo, quindi il
-- valore ATTUALE coincide con quello vero al momento di ciascuna vendita
-- passata: non è una stima, è il dato reale. Da qui in avanti, quando
-- costi/listini cambieranno, saranno le nuove vendite (via codice applicativo,
-- non questo script) a congelare il valore corretto dell'epoca.
--
-- Replica ESATTAMENTE la stessa logica già in uso nel backend:
--  - food_cost_unitario: ricette.costo_ricetta_reale se id_ricetta, altrimenti
--    articoli.prezzo_acquisto_netto se id_prodotto_commerciale (routers/
--    statistiche.py, stat_vendite_aggregate).
--  - prezzo_singolo_lordo: prezzo_singolo * (1 + aliquota IVA / 100), aliquota
--    da ricette.id_iva_vendita / articoli.id_iva_rivendita -> tabella iva
--    (routers/vendite.py, GET /api/vendite/). Resta NULL se prezzo_singolo o
--    l'aliquota non sono noti, esattamente come nel calcolo live.
--  - i "totale" si ottengono SEMPRE dall'unitario già arrotondato * quantita,
--    mai da un arrotondamento indipendente (stessa convenzione di
--    prezzo_totale rispetto a prezzo_singolo).
--
-- Idempotente per costruzione: aggiorna solo le righe dove le nuove colonne
-- sono ancora NULL, quindi non sovrascrive mai un valore già presente (che
-- sia stato scritto da questo stesso script o da una vendita inserita nel
-- frattempo con il nuovo codice applicativo).
-- ============================================================================

BEGIN;

UPDATE public.vendite v
SET
  food_cost_unitario   = fc.food_cost_unitario,
  food_cost_totale     = ROUND((fc.food_cost_unitario * v.quantita)::numeric, 2),
  prezzo_singolo_lordo = fc.prezzo_singolo_lordo,
  prezzo_totale_lordo  = ROUND((fc.prezzo_singolo_lordo * v.quantita)::numeric, 2)
FROM (
  SELECT
    vv.id,
    COALESCE(r.costo_ricetta_reale, a.prezzo_acquisto_netto) AS food_cost_unitario,
    CASE
      WHEN vv.prezzo_singolo IS NOT NULL AND iva.iva IS NOT NULL
        THEN ROUND((vv.prezzo_singolo * (1 + iva.iva / 100.0))::numeric, 2)
      ELSE NULL
    END AS prezzo_singolo_lordo
  FROM public.vendite vv
  LEFT JOIN public.ricette  r   ON r.id = vv.id_ricetta
  LEFT JOIN public.articoli a   ON a.id = vv.id_prodotto_commerciale
  LEFT JOIN public.iva      iva ON iva.id = COALESCE(r.id_iva_vendita, a.id_iva_rivendita)
) fc
WHERE v.id = fc.id
  AND v.food_cost_unitario IS NULL
  AND v.prezzo_singolo_lordo IS NULL;

COMMIT;

-- Verifica (facoltativa, dopo il commit): quante righe restano senza food
-- cost / lordo, e perché (prodotto non più a catalogo, prezzo/aliquota
-- mancanti sul prodotto, ecc.) — non deve preoccupare, sono gli stessi casi
-- che oggi il calcolo live lascia scoperti.
-- SELECT count(*) FILTER (WHERE food_cost_unitario IS NULL) AS senza_food_cost,
--        count(*) FILTER (WHERE prezzo_singolo_lordo IS NULL) AS senza_lordo
-- FROM public.vendite;
