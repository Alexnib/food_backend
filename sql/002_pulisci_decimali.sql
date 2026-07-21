-- Arrotonda a 2 decimali i valori numerici già salvati nelle tabelle
-- vendite, vendite_sospese, articoli, ricette, che in passato potevano
-- essere scritti con decimali periodici (es. divisioni per 1.1 non
-- arrotondate: prezzo_totale/quantita, oppure lordo/(1+iva/100)).
-- Idempotente: ROUND su un numero già a 2 decimali non lo modifica,
-- quindi puoi rieseguirlo in sicurezza.

BEGIN;

UPDATE vendite
SET prezzo_singolo = ROUND(prezzo_singolo::numeric, 2),
    prezzo_totale  = ROUND(prezzo_totale::numeric, 2)
WHERE prezzo_singolo IS DISTINCT FROM ROUND(prezzo_singolo::numeric, 2)
   OR prezzo_totale  IS DISTINCT FROM ROUND(prezzo_totale::numeric, 2);

UPDATE vendite_sospese
SET prezzo_singolo = ROUND(prezzo_singolo::numeric, 2),
    prezzo_totale  = ROUND(prezzo_totale::numeric, 2)
WHERE prezzo_singolo IS DISTINCT FROM ROUND(prezzo_singolo::numeric, 2)
   OR prezzo_totale  IS DISTINCT FROM ROUND(prezzo_totale::numeric, 2);

UPDATE articoli
SET prezzo_acquisto_lordo = ROUND(prezzo_acquisto_lordo::numeric, 2),
    prezzo_acquisto_netto = ROUND(prezzo_acquisto_netto::numeric, 2),
    prezzo_vendita_lordo  = ROUND(prezzo_vendita_lordo::numeric, 2),
    prezzo_vendita_netto  = ROUND(prezzo_vendita_netto::numeric, 2),
    margine               = ROUND(margine::numeric, 2),
    margine_perc          = ROUND(margine_perc::numeric, 2)
WHERE prezzo_acquisto_lordo IS DISTINCT FROM ROUND(prezzo_acquisto_lordo::numeric, 2)
   OR prezzo_acquisto_netto IS DISTINCT FROM ROUND(prezzo_acquisto_netto::numeric, 2)
   OR prezzo_vendita_lordo  IS DISTINCT FROM ROUND(prezzo_vendita_lordo::numeric, 2)
   OR prezzo_vendita_netto  IS DISTINCT FROM ROUND(prezzo_vendita_netto::numeric, 2)
   OR margine              IS DISTINCT FROM ROUND(margine::numeric, 2)
   OR margine_perc         IS DISTINCT FROM ROUND(margine_perc::numeric, 2);

UPDATE ricette
SET prezzo_vendita_lordo = ROUND(prezzo_vendita_lordo::numeric, 2),
    prezzo_vendita_netto = ROUND(prezzo_vendita_netto::numeric, 2),
    costo_ricetta_reale  = ROUND(costo_ricetta_reale::numeric, 2)
WHERE prezzo_vendita_lordo IS DISTINCT FROM ROUND(prezzo_vendita_lordo::numeric, 2)
   OR prezzo_vendita_netto IS DISTINCT FROM ROUND(prezzo_vendita_netto::numeric, 2)
   OR costo_ricetta_reale  IS DISTINCT FROM ROUND(costo_ricetta_reale::numeric, 2);

COMMIT;

-- Verifica (facoltativa, da eseguire DOPO il commit): non deve restituire righe.
-- SELECT id, prezzo_singolo, prezzo_totale FROM vendite
--   WHERE prezzo_singolo IS DISTINCT FROM ROUND(prezzo_singolo::numeric, 2)
--      OR prezzo_totale  IS DISTINCT FROM ROUND(prezzo_totale::numeric, 2);
