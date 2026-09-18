-- Storico dei prezzi d'acquisto per articolo: una riga per ogni variazione di
-- prezzo (netto, lordo o aliquota IVA). Il prezzo corrente resta su
-- articoli.prezzo_acquisto_*; qui va la storia. La scrittura delle righe sarà
-- affidata a un trigger su articoli (script successivo), non al codice Python.

CREATE TABLE IF NOT EXISTS storico_prezzi_acquisto (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_articolo uuid NOT NULL REFERENCES articoli(id) ON DELETE CASCADE,
    id_sede uuid NOT NULL REFERENCES sedi(id) ON DELETE CASCADE,
    prezzo_netto numeric NOT NULL,
    prezzo_lordo numeric,
    -- Percentuale IVA "congelata" al momento del prezzo (non l'id di iva): lo
    -- storico non deve cambiare se la tabella iva viene modificata in futuro.
    iva_perc numeric,
    fornitore text,
    -- Data effettiva del prezzo: data della fattura, oppure la data di oggi
    -- per import Excel e modifiche manuali.
    data_prezzo date NOT NULL DEFAULT CURRENT_DATE,
    fonte text NOT NULL DEFAULT 'manuale'
        CHECK (fonte IN ('fattura', 'excel', 'manuale', 'iniziale')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Lettura tipica: tutti i prezzi di UN articolo in ordine di data.
CREATE INDEX IF NOT EXISTS idx_storico_prezzi_articolo_data
    ON storico_prezzi_acquisto (id_articolo, data_prezzo);

CREATE INDEX IF NOT EXISTS idx_storico_prezzi_id_sede
    ON storico_prezzi_acquisto (id_sede);

-- Nessuna policy: con RLS attiva e senza policy, la tabella non è leggibile
-- né scrivibile con la chiave anon/authenticated esposta dal browser. Il
-- backend usa la service role key, che bypassa RLS, quindi non cambia nulla
-- per l'app.
ALTER TABLE storico_prezzi_acquisto ENABLE ROW LEVEL SECURITY;
