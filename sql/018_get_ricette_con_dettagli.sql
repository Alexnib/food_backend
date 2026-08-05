-- Lettura affidabile di ricette + ingredienti, bypassando il resource
-- embedding di PostgREST — lo stesso tipo di join annidato (ricette →
-- categoria_prodotti / ingredienti_ricetta → articoli) già osservato
-- inaffidabile altrove in questo progetto dopo che il processo gira per un
-- po' (vedi database/config.py, e get_ricette_con_ingredienti già usata in
-- statistiche.py per lo stesso identico motivo).
--
-- Sintomo tipico senza questa funzione: dopo aver modificato la distinta
-- base di una ricetta (PUT /api/produzione/ricette/{id}), la lista ricette
-- continua a mostrare gli ingredienti VECCHI finché non si ricarica
-- manualmente la pagina — il salvataggio è corretto, ma la lettura
-- immediatamente successiva (embedded join) può restituire dati non ancora
-- aggiornati. Fare il join dentro Postgres invece che tramite PostgREST
-- elimina il problema alla radice.
--
-- routers/produzione.py::get_ricette prova questa funzione per prima
-- (tramite call_rpc_or_none), con fallback identico al comportamento
-- odierno (select con embed) se non ancora eseguita sul DB.
CREATE OR REPLACE FUNCTION get_ricette_con_dettagli(p_id_sede uuid)
RETURNS TABLE (
    id uuid,
    nome_ricetta text,
    descrizione_ricetta text,
    id_categoria_prodotto bigint,
    costo_ricetta_reale numeric,
    prezzo_vendita_lordo numeric,
    prezzo_vendita_netto numeric,
    id_iva_vendita bigint,
    is_cancelled boolean,
    created_at timestamptz,
    categoria_prodotti jsonb,
    ingredienti_ricetta jsonb
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        r.id,
        r.nome_ricetta,
        r.descrizione_ricetta,
        r.id_categoria_prodotto,
        r.costo_ricetta_reale,
        r.prezzo_vendita_lordo,
        r.prezzo_vendita_netto,
        r.id_iva_vendita,
        r.is_cancelled,
        r.created_at,
        CASE WHEN cp.id IS NULL THEN NULL
             ELSE jsonb_build_object('nome_categoria', cp.nome_categoria)
        END AS categoria_prodotti,
        COALESCE(
            (
                SELECT jsonb_agg(jsonb_build_object(
                    'id_materia_prima', ir.id_materia_prima,
                    'quantita_per_kg', ir.quantita_per_kg,
                    'perc_scarto', ir.perc_scarto,
                    'articoli', jsonb_build_object(
                        'nome_articolo', a.nome_articolo,
                        'unita_misura', a.unita_misura,
                        'prezzo_acquisto_netto', a.prezzo_acquisto_netto
                    )
                ))
                FROM ingredienti_ricetta ir
                JOIN articoli a ON a.id = ir.id_materia_prima
                WHERE ir.id_ricetta = r.id
            ),
            '[]'::jsonb
        ) AS ingredienti_ricetta
    FROM ricette r
    LEFT JOIN categoria_prodotti cp ON cp.id = r.id_categoria_prodotto
    WHERE r.id_sede = p_id_sede
      AND r.is_cancelled = false;
$$;

REVOKE ALL ON FUNCTION get_ricette_con_dettagli(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION get_ricette_con_dettagli(uuid) FROM anon;
GRANT EXECUTE ON FUNCTION get_ricette_con_dettagli(uuid) TO service_role;
