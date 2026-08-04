-- Import ricette da Excel (AI): salvataggio atomico di più ricette con i
-- rispettivi ingredienti in un'unica transazione, invece di N chiamate POST
-- separate a /api/produzione/ricette — un fallimento a metà (es. una ricetta
-- su 30) non deve lasciare ricette orfane senza ingredienti o un import
-- "parzialmente" salvato senza che l'utente se ne accorga.
--
-- Stesso principio già usato per save_import_articoli_costi (import materie
-- prime): funzione RPC con SECURITY DEFINER, grant solo a service_role,
-- fallback lato Python (routers/import_produzione.py) a insert singole via
-- l'endpoint esistente /api/produzione/ricette se questa funzione non è
-- ancora stata eseguita sul DB (codice errore PostgREST "PGRST202").
--
-- Riusa ESATTAMENTE la stessa formula di costo di _costo_ingrediente()
-- (routers/produzione.py): resa = 1 - perc_scarto/100, quantità effettiva =
-- quantita_per_kg / resa (o quantita_per_kg se resa <= 0), costo = quantità
-- effettiva * prezzo_acquisto_netto dell'articolo.

CREATE OR REPLACE FUNCTION save_import_ricette(p_id_sede uuid, p_ricette jsonb)
RETURNS TABLE(id_ricetta_creata uuid, nome_ricetta text, costo_ricetta_reale numeric)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_ricetta jsonb;
    v_ingrediente jsonb;
    v_id_ricetta uuid;
    v_costo_totale numeric;
    v_quantita_per_kg numeric;
    v_perc_scarto numeric;
    v_resa numeric;
    v_quantita_effettiva numeric;
    v_prezzo_unitario numeric;
BEGIN
    FOR v_ricetta IN SELECT * FROM jsonb_array_elements(p_ricette)
    LOOP
        v_costo_totale := 0;

        INSERT INTO ricette (
            id_sede, nome_ricetta, descrizione_ricetta, id_categoria_prodotto,
            costo_ricetta_reale, prezzo_vendita_lordo, prezzo_vendita_netto, id_iva_vendita
        ) VALUES (
            p_id_sede,
            v_ricetta->>'nome_ricetta',
            NULLIF(v_ricetta->>'descrizione_ricetta', ''),
            NULLIF(v_ricetta->>'id_categoria_prodotto', '')::bigint,
            0,
            COALESCE((v_ricetta->>'prezzo_vendita_lordo')::numeric, 0),
            COALESCE((v_ricetta->>'prezzo_vendita_netto')::numeric, 0),
            (v_ricetta->>'id_iva_vendita')::bigint
        )
        RETURNING id INTO v_id_ricetta;

        FOR v_ingrediente IN SELECT * FROM jsonb_array_elements(COALESCE(v_ricetta->'ingredienti', '[]'::jsonb))
        LOOP
            v_quantita_per_kg := COALESCE((v_ingrediente->>'quantita_per_kg')::numeric, 0);
            v_perc_scarto := COALESCE((v_ingrediente->>'perc_scarto')::numeric, 0);
            v_resa := 1 - (v_perc_scarto / 100);
            IF v_resa > 0 THEN
                v_quantita_effettiva := v_quantita_per_kg / v_resa;
            ELSE
                v_quantita_effettiva := v_quantita_per_kg;
            END IF;

            SELECT prezzo_acquisto_netto INTO v_prezzo_unitario
            FROM articoli
            WHERE id = (v_ingrediente->>'id_materia_prima')::uuid AND id_sede = p_id_sede;

            v_costo_totale := v_costo_totale + (v_quantita_effettiva * COALESCE(v_prezzo_unitario, 0));

            INSERT INTO ingredienti_ricetta (id_ricetta, id_materia_prima, quantita_per_kg, perc_scarto)
            VALUES (v_id_ricetta, (v_ingrediente->>'id_materia_prima')::uuid, v_quantita_per_kg, v_perc_scarto);
        END LOOP;

        UPDATE ricette SET costo_ricetta_reale = ROUND(v_costo_totale, 2) WHERE id = v_id_ricetta;

        id_ricetta_creata := v_id_ricetta;
        nome_ricetta := v_ricetta->>'nome_ricetta';
        costo_ricetta_reale := ROUND(v_costo_totale, 2);
        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION save_import_ricette(uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION save_import_ricette(uuid, jsonb) FROM anon;
GRANT EXECUTE ON FUNCTION save_import_ricette(uuid, jsonb) TO service_role;
