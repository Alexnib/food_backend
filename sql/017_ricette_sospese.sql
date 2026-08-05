-- Ricette importate da Excel che l'utente NON è riuscito a completare (nome
-- o categoria mancante, o almeno un ingrediente non abbinato a un articolo
-- reale) al momento del salvataggio dell'import — stesso identico ruolo di
-- vendite_sospese per l'import vendite: mai perse, risolvibili in un secondo
-- momento da RicetteSospeseModal.tsx invece di dover rifare l'intero import.
--
-- ingredienti è JSONB (a differenza di vendite_sospese, che è piatta):
-- una ricetta ha una lista di ingredienti, alcuni già risolti a un
-- id_materia_prima reale, altri no — stessa forma di
-- RicettaImportata/IngredienteImportato lato frontend (src/types/produzione.ts),
-- cioè lo stato di revisione dell'import così com'è, senza conversioni.
--
-- Nessuna RPC per l'inserimento: a differenza di save_import_ricette (che
-- calcola food cost e deve garantire atomicità ricetta+ingredienti), un
-- insert qui è un dump grezzo senza calcoli derivati — un
-- INSERT ... VALUES (...), (...) è già atomico di suo, e un eventuale
-- orfano è innocuo (resta una riga in sospeso, cancellabile) a differenza
-- di una ricetta senza ingredienti_ricetta.
CREATE TABLE IF NOT EXISTS ricette_sospese (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    id_sede uuid NOT NULL,
    nome_ricetta text NOT NULL DEFAULT '',
    descrizione_ricetta text,
    id_categoria_prodotto bigint,
    prezzo_vendita_netto numeric NOT NULL DEFAULT 0,
    prezzo_vendita_lordo numeric NOT NULL DEFAULT 0,
    id_iva_vendita bigint,
    ingredienti jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ricette_sospese_id_sede ON ricette_sospese(id_sede);
