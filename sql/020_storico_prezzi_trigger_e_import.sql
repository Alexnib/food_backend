-- Storico prezzi d'acquisto, parte 2: trigger su articoli, punto iniziale per
-- gli articoli già esistenti e funzione di import con "match e aggiorna".
-- Richiede sql/019 (tabella storico_prezzi_acquisto). Sicuro da rieseguire.

-- ---------------------------------------------------------------------------
-- 1) Punto iniziale per ogni articolo già a catalogo (una sola volta: salta
--    gli articoli che hanno già almeno un punto). La data è quella di
--    creazione dell'articolo: è l'unica informazione disponibile.
-- ---------------------------------------------------------------------------
INSERT INTO storico_prezzi_acquisto
    (id_articolo, id_sede, prezzo_netto, prezzo_lordo, iva_perc, fornitore, data_prezzo, fonte)
SELECT a.id, a.id_sede, a.prezzo_acquisto_netto, a.prezzo_acquisto_lordo, i.iva,
       a.fornitore, a.created_at::date, 'iniziale'
FROM articoli a
LEFT JOIN iva i ON i.id = a.id_iva_acquisto
WHERE a.is_cancelled = false
  AND a.id_sede IS NOT NULL
  AND a.prezzo_acquisto_netto > 0
  AND NOT EXISTS (
      SELECT 1 FROM storico_prezzi_acquisto s WHERE s.id_articolo = a.id
  );

-- ---------------------------------------------------------------------------
-- 2) Trigger: ogni volta che il prezzo d'acquisto (netto, lordo o aliquota
--    IVA) di un articolo cambia, o un articolo nasce con un prezzo, scrive un
--    punto nello storico. Copre import, modifica manuale e qualunque
--    percorso futuro.
--
--    Data e fonte del punto arrivano da due parametri di TRANSAZIONE
--    (app.data_prezzo, app.fonte_prezzo) che la funzione di import imposta
--    riga per riga; se assenti (es. modifica manuale) valgono oggi/'manuale'.
--    I prezzi a 0 (default di articoli quando il prezzo non è noto) non
--    vengono registrati: sporcherebbero il grafico.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_storico_prezzi_acquisto()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_iva numeric;
    v_data date;
    v_fonte text;
BEGIN
    IF NEW.id_sede IS NULL OR NEW.prezzo_acquisto_netto IS NULL OR NEW.prezzo_acquisto_netto <= 0 THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE'
       AND NEW.prezzo_acquisto_netto IS NOT DISTINCT FROM OLD.prezzo_acquisto_netto
       AND NEW.prezzo_acquisto_lordo IS NOT DISTINCT FROM OLD.prezzo_acquisto_lordo
       AND NEW.id_iva_acquisto IS NOT DISTINCT FROM OLD.id_iva_acquisto THEN
        RETURN NEW;
    END IF;

    SELECT iva INTO v_iva FROM iva WHERE id = NEW.id_iva_acquisto;
    v_data := COALESCE(NULLIF(current_setting('app.data_prezzo', true), '')::date, CURRENT_DATE);
    v_fonte := COALESCE(NULLIF(current_setting('app.fonte_prezzo', true), ''), 'manuale');

    INSERT INTO storico_prezzi_acquisto
        (id_articolo, id_sede, prezzo_netto, prezzo_lordo, iva_perc, fornitore, data_prezzo, fonte)
    VALUES
        (NEW.id, NEW.id_sede, NEW.prezzo_acquisto_netto, NEW.prezzo_acquisto_lordo, v_iva,
         NEW.fornitore, v_data, v_fonte);

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_articoli_storico_prezzi ON articoli;
CREATE TRIGGER trg_articoli_storico_prezzi
AFTER INSERT OR UPDATE OF prezzo_acquisto_netto, prezzo_acquisto_lordo, id_iva_acquisto ON articoli
FOR EACH ROW EXECUTE FUNCTION trg_storico_prezzi_acquisto();

-- ---------------------------------------------------------------------------
-- 3) Import massivo con "match e aggiorna" (sostituisce l'uso di
--    save_import_articoli_costi, che resta sul DB ma non viene più chiamata).
--
--    p_articoli: array ORDINATO per data di elementi con "op":
--      - {"op":"update","id":<uuid>, ...}      articolo già a catalogo
--      - {"op":"insert","slot":<n>, ...}       nuovo articolo
--      - {"op":"update","slot":<n>, ...}       stesso nuovo articolo ripetuto
--                                              nell'import (data successiva)
--    Un prezzo con data PIÙ VECCHIA dell'ultimo punto storico dell'articolo
--    finisce solo nello storico: il prezzo corrente su articoli resta quello
--    più recente.
--    p_costi: righe da inserire in costi_anno_mese (invariato rispetto a prima).
--    Tutto in un'unica transazione.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION import_articoli_con_storico(
    p_id_sede uuid,
    p_articoli jsonb,
    p_costi jsonb
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_item jsonb;
    v_op text;
    v_id uuid;
    v_slots jsonb := '{}'::jsonb;
    v_data date;
    v_fonte text;
    v_netto numeric;
    v_lordo numeric;
    v_id_iva integer;
    v_old_netto numeric;
    v_ultima date;
    v_iva_perc numeric;
    v_inseriti integer := 0;
    v_aggiornati integer := 0;
    v_solo_storico integer := 0;
    v_costi integer := 0;
    v_ids_cambiati uuid[] := '{}';
BEGIN
    FOR v_item IN SELECT * FROM jsonb_array_elements(COALESCE(p_articoli, '[]'::jsonb)) LOOP
        v_op := v_item->>'op';
        v_data := COALESCE(NULLIF(v_item->>'data_prezzo', '')::date, CURRENT_DATE);
        v_fonte := COALESCE(NULLIF(v_item->>'fonte', ''), 'excel');
        v_netto := (v_item->>'prezzo_acquisto_netto')::numeric;
        v_lordo := (v_item->>'prezzo_acquisto_lordo')::numeric;
        v_id_iva := (v_item->>'id_iva_acquisto')::integer;

        IF v_op = 'insert' THEN
            PERFORM set_config('app.data_prezzo', v_data::text, true);
            PERFORM set_config('app.fonte_prezzo', v_fonte, true);

            INSERT INTO articoli (
                id_sede, nome_articolo, unita_misura,
                prezzo_acquisto_netto, prezzo_acquisto_lordo,
                prezzo_vendita_netto, prezzo_vendita_lordo, margine, margine_perc,
                id_iva_acquisto, id_iva_rivendita, id_categoria_prodotto,
                fornitore, anno, is_materia_prima, is_rivendita
            ) VALUES (
                p_id_sede, v_item->>'nome_articolo', v_item->>'unita_misura',
                v_netto, v_lordo,
                COALESCE((v_item->>'prezzo_vendita_netto')::numeric, 0),
                COALESCE((v_item->>'prezzo_vendita_lordo')::numeric, 0),
                COALESCE((v_item->>'margine')::numeric, 0),
                COALESCE((v_item->>'margine_perc')::numeric, 0),
                v_id_iva, (v_item->>'id_iva_rivendita')::integer,
                (v_item->>'id_categoria_prodotto')::integer,
                v_item->>'fornitore', (v_item->>'anno')::integer,
                COALESCE((v_item->>'is_materia_prima')::boolean, false),
                COALESCE((v_item->>'is_rivendita')::boolean, false)
            ) RETURNING id INTO v_id;

            v_slots := jsonb_set(v_slots, ARRAY[v_item->>'slot'], to_jsonb(v_id::text));
            v_inseriti := v_inseriti + 1;

        ELSE
            v_id := COALESCE((v_item->>'id')::uuid, (v_slots->>(v_item->>'slot'))::uuid);

            SELECT prezzo_acquisto_netto INTO v_old_netto
            FROM articoli WHERE id = v_id AND id_sede = p_id_sede;
            CONTINUE WHEN NOT FOUND;

            SELECT max(data_prezzo) INTO v_ultima
            FROM storico_prezzi_acquisto WHERE id_articolo = v_id;

            IF v_ultima IS NOT NULL AND v_data < v_ultima THEN
                -- Prezzo più vecchio dell'ultimo noto: solo storico.
                SELECT iva INTO v_iva_perc FROM iva WHERE id = v_id_iva;
                INSERT INTO storico_prezzi_acquisto
                    (id_articolo, id_sede, prezzo_netto, prezzo_lordo, iva_perc, fornitore, data_prezzo, fonte)
                SELECT v_id, p_id_sede, v_netto, v_lordo, v_iva_perc,
                       v_item->>'fornitore', v_data, v_fonte
                WHERE NOT EXISTS (
                    SELECT 1 FROM storico_prezzi_acquisto
                    WHERE id_articolo = v_id AND data_prezzo = v_data AND prezzo_netto = v_netto
                );
                v_solo_storico := v_solo_storico + 1;
            ELSE
                PERFORM set_config('app.data_prezzo', v_data::text, true);
                PERFORM set_config('app.fonte_prezzo', v_fonte, true);

                UPDATE articoli SET
                    prezzo_acquisto_netto = v_netto,
                    prezzo_acquisto_lordo = v_lordo,
                    id_iva_acquisto = v_id_iva,
                    fornitore = COALESCE(v_item->>'fornitore', fornitore),
                    anno = COALESCE((v_item->>'anno')::integer, anno),
                    margine = COALESCE((v_item->>'margine')::numeric, margine),
                    margine_perc = COALESCE((v_item->>'margine_perc')::numeric, margine_perc)
                WHERE id = v_id AND id_sede = p_id_sede;

                v_aggiornati := v_aggiornati + 1;
                IF v_old_netto IS DISTINCT FROM v_netto AND NOT (v_id = ANY(v_ids_cambiati)) THEN
                    v_ids_cambiati := array_append(v_ids_cambiati, v_id);
                END IF;
            END IF;
        END IF;
    END LOOP;

    PERFORM set_config('app.data_prezzo', '', true);
    PERFORM set_config('app.fonte_prezzo', '', true);

    INSERT INTO costi_anno_mese (id_sede, id_categoria, note, importo, mese, anno, anno_mese)
    SELECT p_id_sede, (c->>'id_categoria')::uuid, c->>'note', (c->>'importo')::double precision,
           c->>'mese', (c->>'anno')::bigint, c->>'anno_mese'
    FROM jsonb_array_elements(COALESCE(p_costi, '[]'::jsonb)) AS c;
    GET DIAGNOSTICS v_costi = ROW_COUNT;

    RETURN jsonb_build_object(
        'inseriti', v_inseriti,
        'aggiornati', v_aggiornati,
        'solo_storico', v_solo_storico,
        'costi', v_costi,
        'ids_prezzo_cambiato', to_jsonb(v_ids_cambiati)
    );
END;
$$;

-- Solo il backend (service role) può chiamarla, non la chiave pubblica.
REVOKE ALL ON FUNCTION import_articoli_con_storico(uuid, jsonb, jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION import_articoli_con_storico(uuid, jsonb, jsonb) TO service_role;
