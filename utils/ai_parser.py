import os
import time
import json
from google import genai
from google.genai import types
import pandas as pd
from typing import List, Optional
import io
from models.magazzino import ParsedResult, FatturaParseResult

def parse_excel_with_ai(excel_file_bytes: bytes, filename: str, categorie_disponibili: list) -> str:
    """
    Legge il file excel o csv, lo converte in testo e lo invia a Gemini.
    Ritorna la stringa JSON validata.
    """
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(excel_file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(excel_file_bytes))
    except Exception as e:
        raise ValueError(f"Errore nella lettura del file: {str(e)}")

    cat_string = "\n".join([
        f"ID: {c.get('id')} - Nome: {c.get('nome_categoria')} - Tipo: {c.get('tipo_categoria', 'Sconosciuto')}" 
        for c in categorie_disponibili
    ])

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    client = genai.Client(api_key=api_key)
    
    import json
    all_products = []
    chunk_size = 100

    for i in range(0, len(df), chunk_size):
        chunk_df = df.iloc[i:i+chunk_size]
        csv_string = chunk_df.to_csv(index=False)
        
        prompt = f"""
Sei un assistente esperto in ristorazione e magazzino in Italia.
Ti sto per fornire un file CSV (o estratto di Excel) caricato da un ristoratore.
Potrebbe essere disordinato, avere colonne senza nome o dati mancanti.

Il tuo compito è estrarre l'elenco dei prodotti e restituirlo come JSON rispettando il formato richiesto.
Per ogni prodotto:
1. 'nome_prodotto': Estrai o deduci il nome.
2. 'tipo': Valuta attentamente la natura del prodotto. Imposta "Materia Prima" per cibi/bevande usati per cucinare. Imposta "Rivendita" per prodotti venduti così come sono. Imposta "Entrambi" se il prodotto viene sia usato per preparazioni sia venduto direttamente al cliente (es. bibite, vini, birre). Imposta "Costo" per tutto ciò che NON è food/beverage ma è materiale di consumo, attrezzature, pulizia (es. bicchieri di plastica, cannucce, tovaglioli, detersivi, carta igienica).
3. 'unita_misura': Estrai o deduci l'unità di misura (kg, lt, pz).
4. 'iva_perc': Estrai l'IVA se c'è. Se l'IVA manca, applica l'aliquota italiana corretta in base al prodotto (solitamente 10% per alimenti/bevande in ristorazione, o 22%, o 4%).
5. 'costo_netto' e 'costo_lordo': Estraili. Se ne manca uno, calcolalo usando l'IVA. (Lordo = Netto * (1 + iva_perc/100)). Arrotonda sempre a 2 decimali.
6. 'id_categoria': Scegli l'ID della categoria più adatta tra questa lista fornita. Se nessuna si adatta, imposta null.

Lista Categorie Disponibili:
{cat_string}

Dati caricati:
```csv
{csv_string}
```

Ritorna ESCLUSIVAMENTE un JSON valido seguendo lo schema richiesto.
"""

        max_retries = 3
        chunk_result = None
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ParsedResult,
                        temperature=0.1
                    ),
                )
                chunk_result = response.text
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                raise ValueError(f"Errore AI dopo {max_retries} tentativi nel blocco {i}: {str(e)}")
        
        if chunk_result:
            try:
                parsed_chunk = json.loads(chunk_result)
                prodotti = parsed_chunk.get("prodotti", [])
                for p in prodotti:
                    if p.get("costo_netto") is not None:
                        p["costo_netto"] = round(float(p["costo_netto"]), 2)
                    if p.get("costo_lordo") is not None:
                        p["costo_lordo"] = round(float(p["costo_lordo"]), 2)
                all_products.extend(prodotti)
            except Exception as e:
                raise ValueError(f"Errore parsing JSON nel blocco {i}: {str(e)}")

    return json.dumps({"prodotti": all_products})


def parse_fattura_with_ai(files: List[tuple]) -> str:
    """
    Legge una o più fatture di acquisto (PDF o foto) e ne estrae fornitore,
    partita IVA e le righe prodotto, tramite Gemini con input multimodale
    diretto (niente OCR/parsing manuale: il documento va così com'è).

    files: lista di tuple (contenuto_bytes, mime_type), una per ogni file
    caricato. Ritorna la stringa JSON validata secondo lo schema
    FatturaParseResult — {"fornitore", "partita_iva", "prodotti": [...]}.

    Qui NON chiediamo all'AI di indovinare 'tipo' (Materia Prima/Rivendita)
    o la categoria: a differenza dei dati tabellari di un file Excel, una
    riga di fattura da sola non basta a dedurli in modo affidabile (dipende
    da come il ristoratore usa quel prodotto nel proprio menù). Quella
    scelta resta sempre dell'utente nella schermata di conferma, come già
    per l'import da Excel.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    client = genai.Client(api_key=api_key)

    prompt = """
Sei un assistente esperto in contabilità e acquisti per la ristorazione in Italia.
Ti sto fornendo una o più fatture di acquisto (PDF o foto) ricevute da un fornitore.

Individua prima le informazioni di TESTATA del documento:
- 'fornitore': la ragione sociale dell'azienda che ha EMESSO la fattura (il venditore che vende, MAI l'azienda che la riceve/acquista).
- 'partita_iva': la partita IVA del fornitore, se presente.
Se ti ho fornito più fatture di fornitori diversi, usa quelle della fattura con più righe prodotto.

Poi, per OGNI riga/prodotto elencata nel corpo di TUTTE le fatture fornite, estrai:
1. 'nome_prodotto': la descrizione del prodotto/materiale acquistato, ripulita da eventuali codici articolo.
2. 'unita_misura': l'unità di misura del prezzo UNITARIO (kg, g, lt, ml, pz). Se la fattura riporta un prezzo "a cassa"/"a confezione" con più pezzi dentro, calcola il prezzo per la singola unità base (es. prezzo a cassa da 6 bottiglie -> prezzo a bottiglia): non lasciare mai il prezzo dell'intera confezione.
3. 'prezzo_acquisto_netto' e 'prezzo_acquisto_lordo': il prezzo UNITARIO (non il totale di riga, non il totale della fattura). Sulle fatture italiane il prezzo unitario riportato riga per riga è quasi sempre l'IMPONIBILE (netto, IVA esclusa): valorizza in quel caso 'prezzo_acquisto_netto' con quel valore. Se conosci l'aliquota IVA di quella riga, calcola anche 'prezzo_acquisto_lordo' = netto * (1 + iva/100), arrotondato a 2 decimali; altrimenti lascialo null. Se invece il documento indica ESPLICITAMENTE che il prezzo unitario riportato è già IVA inclusa, fai il ragionamento inverso.
4. 'iva_percentuale': l'aliquota IVA di quella riga (es. 4, 10, 22). Se la fattura usa un codice IVA anziché la percentuale, deducila dal riepilogo IVA in fondo al documento.

Regole generali:
- NON includere righe di riepilogo, subtotali, sconti a piè di fattura, spese di trasporto/imballo/bolli, a meno che non siano beni/materiali effettivamente acquistati.
- Se ti ho fornito PIÙ fatture insieme, elaborale TUTTE e restituisci le righe prodotto di ciascuna nello stesso array "prodotti", senza saltarne nessuna.
- Se un valore richiesto non è determinabile con certezza dal documento, lascialo null piuttosto che inventarlo.

Ritorna ESCLUSIVAMENTE un JSON valido seguendo lo schema richiesto. Nessun commento o markdown.
"""

    parts = [prompt]
    for content, mime_type in files:
        parts.append(types.Part.from_bytes(data=content, mime_type=mime_type))

    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                # A differenza dell'import Excel (dati già tabellari, alto
                # volume di righe, dove conta la velocità), qui il volume è
                # basso (poche fatture alla volta) ma il documento è un
                # PDF/foto dal layout reale e variabile da fornitore a
                # fornitore — beneficia di un modello con lettura visiva
                # solida. 'gemini-2.5-pro' non è più disponibile per questo
                # progetto (404 "no longer available to new users"): usiamo
                # lo stesso modello multimodale già in produzione per la
                # scansione scontrini (routers/ai_scanner.py).
                model='gemini-3.1-flash-image-preview',
                contents=parts,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=32768,
                    response_mime_type="application/json",
                    response_schema=FatturaParseResult,
                ),
            )
            parsed = json.loads(response.text)
            for p in parsed.get("prodotti", []):
                if p.get("prezzo_acquisto_netto") is not None:
                    p["prezzo_acquisto_netto"] = round(float(p["prezzo_acquisto_netto"]), 2)
                if p.get("prezzo_acquisto_lordo") is not None:
                    p["prezzo_acquisto_lordo"] = round(float(p["prezzo_acquisto_lordo"]), 2)
            return json.dumps(parsed)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

    raise ValueError(f"Errore AI dopo {max_retries} tentativi: {str(last_error)}")


async def parse_vendite_excel_with_ai_stream(excel_file_bytes: bytes, filename: str):
    """
    Legge il file excel o csv delle vendite, lo converte in testo e lo invia a Gemini.
    Estrae il nome del prodotto, la quantità venduta e la data di vendita.
    Restituisce un generatore asincrono (yield) con aggiornamenti di progresso e il risultato finale.
    """
    import pandas as pd
    import io
    import os
    import json
    import asyncio
    from google import genai
    from google.genai import types
    from models.vendite import ParsedVenditaResult

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(excel_file_bytes))
        else:
            # pd.read_excel senza sheet_name legge SOLO il primo foglio del
            # file, ignorando gli altri in silenzio: con file multi-foglio
            # (es. uno "ricette" e uno "vendite") rischiamo di analizzare il
            # foglio sbagliato. Il nome del foglio non è affidabile (dipende
            # da chi ha esportato il file, e in pratica capita che il foglio
            # vendite non si chiami affatto "vendite"), quindi non ci basiamo
            # su quello: leggiamo TUTTI i fogli e prendiamo quello con più
            # righe di dati. Un log vendite (una riga per transazione) è
            # sempre molto più grande di un catalogo ricette/prodotti, quindi
            # questo distingue i due casi in modo affidabile indipendentemente
            # da come sono chiamati i fogli.
            excel_file = pd.ExcelFile(io.BytesIO(excel_file_bytes))
            fogli = {nome: excel_file.parse(nome) for nome in excel_file.sheet_names}
            nome_foglio_scelto = max(fogli, key=lambda nome: len(fogli[nome]))
            df = fogli[nome_foglio_scelto]
    except Exception as e:
        raise ValueError(f"Errore nella lettura del file: {str(e)}")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    # Uso del client asincrono
    client = genai.Client(api_key=api_key)
    
    all_vendite = []
    chunk_size = 50
    total_rows = len(df)
    total_chunks = (total_rows + chunk_size - 1) // chunk_size

    # Semaphoro per limitare il numero di richieste contemporanee a Gemini (es. max 5)
    sem = asyncio.Semaphore(5)

    async def process_chunk(idx, start_row):
        chunk_df = df.iloc[start_row : start_row + chunk_size]
        csv_string = chunk_df.to_csv(index=False)
        
        prompt = f"""
Sei un assistente esperto in analisi dati per la ristorazione.
Ti sto fornendo un file CSV (o estratto di Excel) caricato da un ristoratore contenente le vendite dei prodotti.
Potrebbe essere disordinato, avere colonne senza nome o avere formati di data vari.

Il tuo compito è estrarre l'elenco delle vendite e restituirlo come un JSON che rispetti questo schema rigorosamente:
{{
  "vendite": [
    {{
      "nome_prodotto_estratto": "Nome del prodotto venduto",
      "quantita": 10.5,
      "data_vendita": "YYYY-MM-DD",
      "prezzo_singolo": 4.5,
      "prezzo_totale": 47.25,
      "prezzo_lordo": false
    }}
  ]
}}

Regole:
1. 'nome_prodotto_estratto': Estrai o deduci chiaramente il nome del prodotto.
2. 'quantita': Numero intero o decimale rappresentante la quantità venduta.
3. 'data_vendita': Trasforma qualsiasi formato di data presente nel file nel formato ISO "YYYY-MM-DD" (es: 2026-07-13). Se non è presente una data in una riga, cerca di dedurla dalle righe precedenti.
4. TASSATIVO: Assicurati di estrarre e mappare OGNI SINGOLA RIGA del file CSV fornitoti. Non raggruppare, non sommare, non filtrare e NON TRALASCIARE nessuna riga per alcun motivo. L'array JSON finale deve avere un numero di elementi pari al numero di righe valide nel CSV.
5. 'prezzo_singolo' e 'prezzo_totale' (OPZIONALI): SOLO se il file contiene colonne di prezzo per quella riga. 'prezzo_singolo' è il prezzo di UNA unità del prodotto; 'prezzo_totale' è il ricavo complessivo della riga (prezzo_singolo * quantita, o un importo già totale presente nel file). Estrai quello/i che trovi così come sono scritti, senza inventarli né calcolarli tu se manca l'informazione: se il file NON ha nessuna colonna riconducibile a un prezzo/importo/ricavo, lascia ENTRAMBI i campi a null. Se trovi solo uno dei due (es. solo il totale di riga, o solo il prezzo unitario), valorizza solo quello e lascia l'altro a null.
6. 'prezzo_lordo' (SOLO se hai estratto un prezzo, altrimenti null): un file di vendite come questo esporta quasi sempre l'incasso REALE per riga — cioè quello che il cliente ha pagato, IVA inclusa (LORDO) — non un prezzo di listino teorico. Per questo motivo, IMPOSTA SEMPRE 'prezzo_lordo' a true per default: il sistema si occupa già di scorporare l'IVA e verificare/correggere il risultato contro il listino del prodotto abbinato, quindi partire dal lordo è la scelta sicura anche quando hai dei dubbi. Fai eccezione (metti 'prezzo_lordo' a false) SOLO se il file dichiara ESPLICITAMENTE e senza ambiguità che quella colonna è già netta/imponibile (es. un'intestazione di colonna che dice letteralmente "netto" o "imponibile") — mai basandoti solo su come "sembrano" i decimali: sia i prezzi netti che quelli lordi possono essere cifre tonde o con centesimi, a seconda di come il gestore ha impostato i prezzi del locale, quindi quell'euristica visiva non è affidabile e non va usata.
7. Se il file contiene PIÙ colonne di importo per la stessa riga, e alcune sono esplicitamente etichettate come lorde/"con IVA" e altre come nette/"netto IVA" (es. intestazioni tipo "Vendite Tot (con iva)" e "Vendite Tot (netto iva)"): questo è il caso in cui hai un'indicazione esplicita del punto 6. Usa SEMPRE la colonna esplicitamente NETTA come prezzo_totale (o prezzo_singolo se è un valore per unità anziché un totale di riga), imposta 'prezzo_lordo' a false, e ignora del tutto la colonna lorda/con IVA: il valore netto è già quello che serve, non va scorporato di nuovo dall'IVA.
8. Ignora completamente colonne che non riguardano la vendita in sé: food cost, margine, categoria/famiglia del prodotto, o colonne di supporto calcolate dalla data (anno, mese, giorno della settimana). Non fanno parte dello schema richiesto: non estrarle, non sommarle e non usarle per dedurre altri campi.

Restituisci SOLO il JSON valido. Nessun commento o markdown.
"""
        # Retry con backoff esponenziale (2s, 4s, 8s, 16s, 32s): un errore 503
        # "modello sovraccarico" da parte di Gemini è quasi sempre temporaneo
        # (pochi secondi/minuti), ma con un'attesa fissa di soli 2s e 3
        # tentativi un blocco può esaurirli prima che il sovraccarico rientri.
        max_retries = 5
        parsed_chunk = None
        last_error = None

        async with sem:
            for attempt in range(max_retries):
                try:
                    response = await client.aio.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[
                            prompt,
                            f"Dati caricati:\n```csv\n{csv_string}\n```"
                        ],
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            max_output_tokens=16384,
                            # Estrazione dati deterministica: nessun ragionamento necessario.
                            # Senza disabilitarlo, il "thinking" di gemini-2.5-flash consuma
                            # una quota variabile dello stesso max_output_tokens, troncando
                            # a volte il JSON finale prima che sia completo (stringhe non
                            # terminate) — da qui gli errori intermittenti "blocco N".
                            thinking_config=types.ThinkingConfig(thinking_budget=0),
                            response_mime_type="application/json",
                            response_schema=ParsedVenditaResult,
                        )
                    )
                    parsed_chunk = json.loads(response.text)
                    break
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue

        if parsed_chunk is not None:
            return {"vendite": parsed_chunk.get("vendite", []), "errore": None}

        # Un blocco fallito non deve far perdere TUTTE le vendite già estratte
        # dagli altri blocchi: lo segnaliamo (con l'intervallo di righe del
        # file coinvolto) invece di sollevare un'eccezione che interromperebbe
        # l'intero import.
        riga_da = start_row + 1
        riga_a = start_row + len(chunk_df)
        return {
            "vendite": [],
            "errore": f"Righe {riga_da}-{riga_a} del file: {str(last_error)}",
        }

    # Creazione dei task
    tasks = []
    for idx, i in enumerate(range(0, total_rows, chunk_size)):
        tasks.append(process_chunk(idx, i))

    completed_chunks = 0
    blocchi_falliti = []
    # Aspettiamo il completamento man mano che finiscono
    for future in asyncio.as_completed(tasks):
        esito = await future
        all_vendite.extend(esito["vendite"])
        if esito["errore"]:
            blocchi_falliti.append(esito["errore"])
        completed_chunks += 1

        # Invio evento di progresso
        progress_pct = int((completed_chunks / total_chunks) * 100)
        yield json.dumps({"progress": progress_pct}) + "\n"

    final_json = json.dumps({"vendite": all_vendite})
    # Validazione Pydantic
    ParsedVenditaResult.model_validate_json(final_json)

    # Invio evento di completamento e risultato finale. Se uno o più blocchi
    # sono falliti (es. sovraccarico temporaneo dell'AI) dopo tutti i
    # tentativi, lo segnaliamo con gli intervalli di righe coinvolti: il resto
    # del file, comunque estratto correttamente, non va perso.
    result_payload = {"vendite": all_vendite}
    if blocchi_falliti:
        result_payload["errori_parziali"] = blocchi_falliti
    yield json.dumps({"result": result_payload}) + "\n"
