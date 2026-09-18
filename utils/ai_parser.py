import os
import time
import json
import asyncio
import logging
from google import genai
from google.genai import types
import pandas as pd
from typing import List, Optional
import io
from models.magazzino import ParsedResult, FatturaParseResult
from models.produzione import ParsedRicetteResult
from utils.articoli_match import data_prezzo_valida

logger = logging.getLogger(__name__)

# Timeout per singola chiamata Gemini: prima di questa modifica nessuna delle
# funzioni di questo file ne aveva uno (confermato: `generate_content` senza
# `http_options` usa il timeout di default dell'SDK, che di fatto è "nessuno"
# per le richieste più lente), quindi un modello sovraccarico o un problema di
# rete poteva lasciare una richiesta appesa indefinitamente, senza feedback
# per l'utente e senza modo di riprovare. I blocchi testuali (Excel) sono più
# leggeri dei documenti multimodali (scontrini/fatture): due soglie diverse.
#
# _TIMEOUT_TESTO_MS era 45s: in un caso reale, due blocchi di un import
# materie prime hanno esaurito tutti i 5 tentativi con lo stesso identico
# errore Gemini (504 DEADLINE_EXCEEDED) — segno che 45s erano troppo pochi
# per quei blocchi (non un blip transitorio: se lo fosse stato, sarebbe
# bastato UN retry a risolverlo). Alzato a 90s per dare più respiro a
# ciascun tentativo; per restare comunque sotto il timeout lato frontend di
# useImportMagazzino.ts (300s), il numero di tentativi in parse_excel_with_ai
# è stato ridotto in cambio (vedi commento lì) — più tempo a testa, meno
# tentativi, invece di tanti tentativi troppo brevi per essere utili.
_TIMEOUT_TESTO_MS = 90_000
_TIMEOUT_MULTIMODALE_MS = 60_000

# Righe massime per import Excel (vendite e materie prime): un file più
# grande di così va diviso in più caricamenti — evita tempi di elaborazione e
# costi Gemini illimitati su un singolo upload (prima di questa modifica non
# esisteva alcun limite in nessuno dei due importatori).
MAX_RIGHE_EXCEL = 5000

# Ricette massime per import: a differenza di materie prime/vendite (chunk a
# righe fisse), qui si chunka per ricette complete (vedi sotto) — un tetto
# sul NUMERO di ricette, non solo sulle righe totali, evita comunque un
# singolo upload con un numero di chiamate Gemini illimitato.
MAX_RICETTE_EXCEL = 300

# Fatture caricabili in un solo batch: parse_fattura_with_ai fa UNA sola
# chiamata Gemini con tutti i file insieme, con max_output_tokens=32768 fisso
# — troppe fatture con troppe righe prodotto totali rischiano di troncare il
# JSON di risposta a metà prima che sia completo. 20 file, con fatture reali
# (10-30 righe l'una), resta con ampio margine sotto quella soglia.
MAX_FILE_FATTURA = 20


# Istruzione per il punto 5 del prompt (prezzi), diversa a seconda di cosa
# l'utente ha dichiarato contenere il file — prima di questa modifica l'AI
# doveva SEMPRE indovinare se una colonna prezzo isolata fosse netta o
# lorda, senza alcun contesto: dirglielo in anticipo toglie l'ambiguità e
# riduce gli errori di interpretazione. "entrambi" resta il comportamento
# di sempre (comportamento di default se il chiamante non specifica nulla),
# solo reso più esplicito sul fatto che vanno cercate due colonne separate
# invece di dedurne una dall'altra quando entrambe sono presenti.
_ISTRUZIONI_PREZZO = {
    "lordo": (
        "5. 'costo_netto' e 'costo_lordo': il file contiene SOLO il prezzo LORDO "
        "(IVA inclusa) — la colonna prezzo che trovi è sempre il lordo. Valorizza "
        "'costo_lordo' con quel valore così com'è scritto, poi calcola 'costo_netto' "
        "scorporando l'IVA (Netto = Lordo / (1 + iva_perc/100)). NON esiste una "
        "colonna netta separata in questo file: non cercarla, non confonderla con "
        "altre colonne numeriche. Arrotonda sempre a 2 decimali."
    ),
    "netto": (
        "5. 'costo_netto' e 'costo_lordo': il file contiene SOLO il prezzo NETTO "
        "(IVA esclusa, imponibile) — la colonna prezzo che trovi è sempre il netto. "
        "Valorizza 'costo_netto' con quel valore così com'è scritto, poi calcola "
        "'costo_lordo' aggiungendo l'IVA (Lordo = Netto * (1 + iva_perc/100)). NON "
        "esiste una colonna lorda separata in questo file: non cercarla, non "
        "confonderla con altre colonne numeriche. Arrotonda sempre a 2 decimali."
    ),
    "entrambi": (
        "5. 'costo_netto' e 'costo_lordo': il file contiene ENTRAMBI i prezzi in "
        "colonne separate (una IVA esclusa, l'altra IVA inclusa) — individua le due "
        "colonne corrispondenti ed estrai i valori così come sono scritti, senza "
        "calcolarli tu. Solo se per una riga manca uno dei due valori, calcolalo "
        "dall'altro usando l'IVA (Lordo = Netto * (1 + iva_perc/100)). Arrotonda "
        "sempre a 2 decimali."
    ),
}

# Istruzione per il prezzo di VENDITA delle ricette (diverso dal prezzo di
# ACQUISTO delle materie prime sopra): qui non calcoliamo mai lordo<->netto
# nel prompt, perché l'aliquota IVA di vendita non viene dal file ma scelta
# dall'utente nella schermata di revisione — se il file ne fornisce solo uno
# dei due, l'altro resta null e viene calcolato lato frontend con l'IVA
# scelta lì, non qui.
_ISTRUZIONI_PREZZO_VENDITA = {
    "lordo": (
        "il file contiene SOLO il prezzo di vendita LORDO (IVA inclusa) — "
        "valorizza 'prezzo_vendita_lordo' con quel valore così com'è scritto "
        "e lascia 'prezzo_vendita_netto' a null (verrà calcolato altrove con "
        "l'aliquota scelta dall'utente). Arrotonda a 2 decimali."
    ),
    "netto": (
        "il file contiene SOLO il prezzo di vendita NETTO (IVA esclusa) — "
        "valorizza 'prezzo_vendita_netto' con quel valore così com'è scritto "
        "e lascia 'prezzo_vendita_lordo' a null. Arrotonda a 2 decimali."
    ),
    "entrambi": (
        "il file contiene ENTRAMBI i prezzi di vendita in colonne separate "
        "(uno IVA esclusa, l'altro IVA inclusa) — individua le due colonne ed "
        "estrai i valori così come sono scritti, senza calcolarli tu. Se per "
        "una ricetta manca uno dei due valori, lascialo null. Arrotonda a 2 "
        "decimali."
    ),
}


async def parse_excel_with_ai_stream(excel_file_bytes: bytes, filename: str, categorie_disponibili: list, tipo_prezzo: str = "entrambi"):
    """
    Legge il file excel o csv delle materie prime/articoli, lo converte in
    testo e lo invia a Gemini a blocchi. Restituisce un generatore asincrono
    (yield) con aggiornamenti di progresso ({"progress": pct}) e infine il
    risultato completo ({"result": {...}}) — stesso schema NDJSON di
    parse_vendite_excel_with_ai_stream, per poter mostrare all'utente una
    barra di avanzamento reale invece di uno spinner senza percentuale.

    Elaborazione parallela (stesso modello di parse_vendite_excel_with_ai_stream,
    blocchi da 50 righe con concorrenza limitata a 5): la versione precedente
    era sequenziale con retry a `time.sleep()` bloccante, il che aveva due
    problemi — tempi lineari con la dimensione del file, e un `time.sleep()`
    dentro una route `async def` senza thread-pool blocca l'intero event loop
    del server per la durata dell'intera elaborazione, rallentando anche le
    richieste di altri utenti nel frattempo. Un blocco che esaurisce i retry
    non manda più in errore l'intero import (prima: tutti i blocchi già
    estratti con successo venivano scartati) — viene segnalato in
    "errori_parziali" e il resto dei blocchi continua comunque.

    tipo_prezzo: "lordo" | "netto" | "entrambi" — dichiarato dall'utente in
    fase di caricamento su cosa contiene il file, per non far indovinare
    all'AI se una colonna prezzo isolata sia netta o lorda (vedi
    _ISTRUZIONI_PREZZO).
    """
    if tipo_prezzo not in _ISTRUZIONI_PREZZO:
        raise ValueError(f"tipo_prezzo non valido: '{tipo_prezzo}' (atteso: lordo, netto o entrambi).")

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(excel_file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(excel_file_bytes))
    except Exception as e:
        logger.warning("Lettura file fallita: %s", e)
        raise ValueError("Impossibile leggere il file: formato non valido o file danneggiato.")

    if len(df) > MAX_RIGHE_EXCEL:
        raise ValueError(
            f"Il file ha {len(df)} righe, oltre il limite di {MAX_RIGHE_EXCEL}: dividilo in più caricamenti."
        )

    cat_string = "\n".join([
        f"ID: {c.get('id')} - Nome: {c.get('nome_categoria')} - Tipo: {c.get('tipo_categoria', 'Sconosciuto')}"
        for c in categorie_disponibili
    ])
    istruzione_prezzo = _ISTRUZIONI_PREZZO[tipo_prezzo]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    client = genai.Client(api_key=api_key)

    chunk_size = 50
    total_rows = len(df)
    total_chunks = (total_rows + chunk_size - 1) // chunk_size
    sem = asyncio.Semaphore(5)

    async def process_chunk(start_row):
        chunk_df = df.iloc[start_row:start_row + chunk_size]
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
{istruzione_prezzo}
6. 'id_categoria': Scegli l'ID della categoria più adatta tra questa lista fornita. Se nessuna si adatta, imposta null.

Lista Categorie Disponibili:
{cat_string}

Dati caricati:
```csv
{csv_string}
```

Ritorna ESCLUSIVAMENTE un JSON valido seguendo lo schema richiesto.
"""

        # Retry con backoff esponenziale, non bloccante (asyncio.sleep), per
        # assorbire un 503/504 temporaneo (es. "modello sovraccarico"). Prima
        # erano solo 3 tentativi per stare sotto un timeout fisso lato
        # frontend (axios, 300s): ora che l'upload passa da una fetch/stream
        # NDJSON senza timeout fisso (vedi magazzinoService.uploadExcelImport),
        # quel vincolo non c'è più — allineato a 5 tentativi come
        # parse_vendite_excel_with_ai_stream, per dare più margine a un blocco
        # prima di arrendersi (vedi anche il fallimento totale più sotto: un
        # blocco che esaurisce anche questi tentativi fa fallire l'intero
        # import, quindi vale la pena insistere di più qui).
        max_retries = 5
        chunk_result = None
        last_error = None

        async with sem:
            for attempt in range(max_retries):
                try:
                    response = await client.aio.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=ParsedResult,
                            temperature=0.1,
                            http_options=types.HttpOptions(timeout=_TIMEOUT_TESTO_MS),
                        ),
                    )
                    chunk_result = response.text
                    break
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue

        if chunk_result is None:
            riga_da = start_row + 1
            riga_a = start_row + len(chunk_df)
            logger.error("Import materie prime, righe %s-%s: %s", riga_da, riga_a, last_error)
            return {"prodotti": [], "errore": f"Righe {riga_da}-{riga_a} del file: il servizio AI non ha risposto correttamente."}

        try:
            parsed_chunk = json.loads(chunk_result)
        except Exception as e:
            riga_da = start_row + 1
            riga_a = start_row + len(chunk_df)
            logger.error("Import materie prime, righe %s-%s: risposta AI non interpretabile: %s", riga_da, riga_a, e)
            return {"prodotti": [], "errore": f"Righe {riga_da}-{riga_a} del file: risposta AI non interpretabile."}

        prodotti = parsed_chunk.get("prodotti", [])
        for p in prodotti:
            if p.get("costo_netto") is not None:
                p["costo_netto"] = round(float(p["costo_netto"]), 2)
            if p.get("costo_lordo") is not None:
                p["costo_lordo"] = round(float(p["costo_lordo"]), 2)
        return {"prodotti": prodotti, "errore": None}

    tasks = [process_chunk(i) for i in range(0, total_rows, chunk_size)]
    all_products = []
    blocchi_falliti = []
    completed_chunks = 0
    for future in asyncio.as_completed(tasks):
        esito = await future
        all_products.extend(esito["prodotti"])
        if esito["errore"]:
            logger.warning("parse_excel_with_ai_stream: blocco fallito - %s", esito["errore"])
            blocchi_falliti.append(esito["errore"])
        completed_chunks += 1

        progress_pct = int((completed_chunks / total_chunks) * 100)
        yield json.dumps({"progress": progress_pct}) + "\n"

    if blocchi_falliti:
        # Mai consegnare un'estrazione parziale: se anche un solo blocco non
        # è stato analizzato dopo tutti i tentativi, l'intero import fallisce
        # e va ripetuto da capo, invece di far arrivare in tabella un
        # risultato con alcune righe silenziosamente mancanti che l'utente
        # potrebbe non notare e salvare per sbaglio. Il router (vedi
        # import_magazzino.py) intercetta questa eccezione e la inoltra come
        # evento {"error": ...} nello stream, esattamente come un errore di
        # validazione a monte.
        raise ValueError("Impossibile analizzare l'intero file: " + "; ".join(blocchi_falliti))

    result_payload = {"prodotti": all_products}
    yield json.dumps({"result": result_payload}) + "\n"


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
    if len(files) > MAX_FILE_FATTURA:
        raise ValueError(f"Troppi file in un solo caricamento ({len(files)}): il limite è {MAX_FILE_FATTURA}.")

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
5. 'data_documento': la data di EMISSIONE della fattura a cui appartiene quella riga, in formato YYYY-MM-DD (es. 2026-09-03). Con più fatture, ogni riga porta la data della SUA fattura. Usa la data della fattura, non la data di scadenza del pagamento né la data di consegna; se non è leggibile con certezza lasciala null.

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
                    http_options=types.HttpOptions(timeout=_TIMEOUT_MULTIMODALE_MS),
                ),
            )
            parsed = json.loads(response.text)
            for p in parsed.get("prodotti", []):
                if p.get("prezzo_acquisto_netto") is not None:
                    p["prezzo_acquisto_netto"] = round(float(p["prezzo_acquisto_netto"]), 2)
                if p.get("prezzo_acquisto_lordo") is not None:
                    p["prezzo_acquisto_lordo"] = round(float(p["prezzo_acquisto_lordo"]), 2)
                # Data della fattura per lo storico prezzi d'acquisto: se manca,
                # non è valida o è futura si usa oggi (vedi data_prezzo_valida).
                p["data_documento"] = data_prezzo_valida(p.get("data_documento")).isoformat()
            return json.dumps(parsed)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

    logger.error("Analisi fattura fallita dopo %s tentativi: %s", max_retries, last_error)
    raise ValueError(f"L'analisi della fattura non è riuscita dopo {max_retries} tentativi: il servizio AI non ha risposto correttamente. Riprova tra qualche minuto.")


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
        logger.warning("Lettura file fallita: %s", e)
        raise ValueError("Impossibile leggere il file: formato non valido o file danneggiato.")

    if len(df) > MAX_RIGHE_EXCEL:
        raise ValueError(
            f"Il file ha {len(df)} righe, oltre il limite di {MAX_RIGHE_EXCEL}: dividilo in più caricamenti."
        )

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

    # Stessa protezione già usata per le ricette: un "|" letterale dentro un
    # valore romperebbe le colonne della tabella Markdown.
    def _escape_pipe(testo) -> str:
        return str(testo).replace("|", "/")

    async def process_chunk(idx, start_row):
        chunk_df = df.iloc[start_row : start_row + chunk_size]

        # Tabella Markdown invece del CSV grezzo, con una colonna "Riga"
        # esplicita — stessa modifica già fatta per le ricette (vedi
        # parse_ricette_excel_with_ai_stream): è quello che ha risolto lì un
        # bug reale in cui l'AI, a parità di altri campi, fondeva/deduplicava
        # righe che sembravano ripetute (es. stesso prodotto venduto più
        # volte nello stesso giorno). Un numero di riga esplicito rende ogni
        # riga strutturalmente distinta anche quando i valori sono identici,
        # cosa che nessuna istruzione testuale da sola è bastata a garantire
        # in modo robusto a scala reale.
        intestazioni = [_escape_pipe(c) for c in chunk_df.columns]
        riga_intestazione = "| Riga | " + " | ".join(intestazioni) + " |"
        riga_separatore = "|---|" + "---|" * len(intestazioni)
        righe_tabella = []
        for j, (_, riga) in enumerate(chunk_df.iterrows()):
            valori = ["" if pd.isna(v) else _escape_pipe(v) for v in riga]
            righe_tabella.append(f"| {j + 1} | " + " | ".join(valori) + " |")
        tabella_md = "\n".join([riga_intestazione, riga_separatore] + righe_tabella)

        prompt = f"""
Sei un assistente esperto in analisi dati per la ristorazione.
Ti sto fornendo una tabella con le vendite dei prodotti, caricata da un ristoratore a partire da un file Excel o CSV e formatta in Markdown.
Potrebbe essere disordinata, avere colonne senza nome o avere formati di data vari.
Analizza il contenuto RIGA per RIGA, usando la colonna "Riga" per riferirti a ciascuna riga senza ambiguità.

Il tuo compito è estrarre l'elenco delle vendite e restituirlo come un JSON che rispetti questo schema rigorosamente:
{{
  "vendite": [
    {{
      "nome_prodotto_estratto": "Nome del prodotto venduto",
      "quantita": 3,
      "data_vendita": "YYYY-MM-DD",
      "prezzo_totale_lordo": 60.0,

    }}
  ]
}}

Regole:
DI PRIMARIA IMPORTANZA: Il campo prezzo_totale_lordo deve essere SEMPRE valorizzato, non può mai essere null
1. 'nome_prodotto_estratto': Estrai o deduci chiaramente il nome del prodotto.
2. 'quantita': Numero intero o decimale rappresentante la quantità venduta.
3. 'data_vendita': Trasforma qualsiasi formato di data presente nel file nel formato ISO "YYYY-MM-DD" (es: 2026-07-13). Se non è presente una data in una riga, cerca di dedurla dalle righe precedenti.
4. TASSATIVO: Assicurati di estrarre e mappare OGNI SINGOLA RIGA della tabella fornita, identificata dal numero in colonna "Riga". Non raggruppare, non sommare, non filtrare e NON TRALASCIARE nessuna riga per alcun motivo, anche se due righe sembrano identiche o quasi identiche in tutti i campi: righe con un numero di "Riga" diverso sono SEMPRE righe diverse e vanno SEMPRE riportate entrambe. L'array JSON finale deve avere un numero di elementi pari al numero di righe valide della tabella.
5. Se il file contiene PIÙ colonne di importo per la stessa riga (es. una "lorda"/"con IVA" e una "netta"/"imponibile" affiancate): estrai il valore dalla colonna LORDA/con IVA come prezzo_totale_lordo e ignora del tutto quella netta
6. Ignora completamente colonne che non riguardano la vendita in sé: food cost, margine, categoria/famiglia del prodotto, o colonne di supporto calcolate dalla data (anno, mese, giorno della settimana). Non fanno parte dello schema richiesto: non estrarle, non sommarle e non usarle per dedurre altri campi. La colonna "Riga" stessa non fa parte dello schema: serve solo per riferirti alle righe, non va riportata nell'output.

Tabella:
{tabella_md}

Restituisci SOLO il JSON valido. Nessun commento o markdown.
"""

        # Log temporaneo su richiesta esplicita (stessa esigenza già chiesta
        # per le ricette): print(), non logger, perché non esiste ancora
        # nessuna configurazione di logging nel progetto. Da togliere quando
        # non serve più.
        print(f"\n{'=' * 80}\n[VENDITE] PROMPT INVIATO A GEMINI (chunk {idx}):\n{'=' * 80}\n{prompt}\n{'=' * 80}\n")
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
                        contents=prompt,
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
                            http_options=types.HttpOptions(timeout=_TIMEOUT_TESTO_MS),
                        )
                    )
                    parsed_chunk = json.loads(response.text)
                    print(f"\n{'=' * 80}\n[VENDITE] RISPOSTA GEMINI (chunk {idx}, tentativo {attempt + 1}):\n{'=' * 80}\n{response.text}\n{'=' * 80}\n")
                    break
                except Exception as e:
                    last_error = e
                    print(f"\n[VENDITE] ERRORE al tentativo {attempt + 1} (chunk {idx}): {e}\n")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue

        if parsed_chunk is not None:
            return {"vendite": parsed_chunk.get("vendite", []), "errore": None}

        # Non solleviamo subito un'eccezione qui: lasciamo che asyncio.as_completed
        # più sotto raccolga comunque il progresso di TUTTI i blocchi (compresi
        # quelli riusciti) prima di decidere se far fallire l'intero import.
        riga_da = start_row + 1
        riga_a = start_row + len(chunk_df)
        return {
            "vendite": [],
            "errore": f"Righe {riga_da}-{riga_a} del file: il servizio AI non ha risposto correttamente.",
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

    if blocchi_falliti:
        # Mai consegnare un'estrazione parziale: se anche un solo blocco non
        # è stato analizzato dopo tutti i tentativi (es. sovraccarico
        # temporaneo dell'AI), l'intero import fallisce e va ripetuto da
        # capo, invece di far arrivare in tabella un risultato con alcune
        # vendite silenziosamente mancanti che l'utente potrebbe non notare
        # e salvare per sbaglio. Il router (routers/vendite.py) intercetta
        # questa eccezione e la inoltra come evento {"error": ...} nello
        # stream, esattamente come un errore di validazione a monte.
        raise ValueError("Impossibile analizzare l'intero file: " + "; ".join(blocchi_falliti))

    final_json = json.dumps({"vendite": all_vendite})
    # Validazione Pydantic
    ParsedVenditaResult.model_validate_json(final_json)

    result_payload = {"vendite": all_vendite}
    yield json.dumps({"result": result_payload}) + "\n"


def _testo_a_float(testo) -> Optional[float]:
    """Converte in float un testo numerico, tollerando la virgola italiana
    come separatore decimale (es. "634,75") oltre al punto standard."""
    try:
        return float(testo)
    except (ValueError, TypeError):
        pass
    try:
        return float(str(testo).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _correggi_quantita_con_originali(ricette_estratte: list, blocchi_chunk: list) -> None:
    """
    Sostituisce 'quantita' di ogni ingrediente estratto con il valore ESATTO
    già letto in modo deterministico dal file in Python (blocchi_chunk),
    invece di fidarsi del numero ritrascritto dall'AI — in prova, con file
    reali, capitava che l'AI restituisse valori diversi da quelli scritti
    nel file (in un caso reale, "normalizzava" le quantità di una ricetta
    fino a farle sommare esattamente a 1000, come se applicasse da sé una
    convenzione "grammi per kg" invece di copiare i numeri dati). La
    quantità non ha bisogno dell'AI per essere letta: la conosciamo già
    riga per riga da prima ancora di chiamarla; l'AI serve solo per il nome
    ripulito dell'ingrediente, la categoria e il raggruppamento — non per
    "leggere" un numero che abbiamo già in mano.

    L'abbinamento ricetta-estratta -> blocco-originale avviene per
    'id_blocco' (l'indice del blocco nel chunk, che l'AI deve solo
    RIPORTARE, non interpretare) — non per nome: abbinare per nome falliva
    silenziosamente ogni volta che l'AI "ripuliva" il nome anche di un solo
    carattere (richiesto esplicitamente dal punto 1 del prompt), lasciando
    silenziosamente le quantità sbagliate dell'AI al posto di quelle vere.

    Applicata solo se il numero di ingredienti restituiti per una ricetta
    coincide con quello originale (altrimenti l'AI ha saltato/aggiunto
    righe, un problema diverso da questo, e non tocchiamo nulla per non
    peggiorare un disallineamento già presente).
    """
    for ricetta in ricette_estratte:
        id_blocco = ricetta.get("id_blocco")
        if not isinstance(id_blocco, int) or id_blocco < 0 or id_blocco >= len(blocchi_chunk):
            print(f"[RICETTE] id_blocco mancante o non valido per '{ricetta.get('nome_ricetta')}': {id_blocco!r} — quantità NON corrette per questa ricetta.")
            continue
        blocco_originale = blocchi_chunk[id_blocco]

        ingredienti_estratti = ricetta.get("ingredienti") or []
        righe_originali = blocco_originale["righe"]
        if len(ingredienti_estratti) != len(righe_originali):
            print(
                f"[RICETTE] '{ricetta.get('nome_ricetta')}' (id_blocco={id_blocco}): "
                f"{len(ingredienti_estratti)} ingredienti restituiti dall'AI contro "
                f"{len(righe_originali)} nel file — quantità NON corrette per questa ricetta."
            )
            continue

        for ing_estratto, riga_originale in zip(ingredienti_estratti, righe_originali):
            testo = riga_originale["quantita_testo"]
            if testo == "MANCANTE":
                continue
            valore_originale = _testo_a_float(testo)
            if valore_originale is not None:
                ing_estratto["quantita"] = valore_originale

        ricetta.pop("id_blocco", None)  # riferimento interno: non deve arrivare al frontend


async def parse_ricette_excel_with_ai_stream(
    excel_file_bytes: bytes,
    filename: str,
    categorie_disponibili: list,
    prezzo_vendita_presente: bool = False,
    tipo_prezzo_vendita: str = "entrambi",
):
    """
    Legge il file excel/csv delle ricette e lo invia a Gemini a blocchi.
    Restituisce un generatore asincrono (yield) con aggiornamenti di
    progresso ({"progress": pct}) e infine il risultato completo
    ({"result": {...}}) — stesso schema NDJSON degli altri importatori,
    stesso comportamento "tutto o niente" su un blocco che esaurisce i
    retry (vedi parse_excel_with_ai_stream).

    A differenza degli altri due importatori, qui il file è "melted": una
    riga per ingrediente, più righe consecutive condividono lo stesso nome
    ricetta. Chunkare a righe fisse (come materie prime/vendite) rischierebbe
    di spezzare una ricetta a metà tra due chiamate AI parallele — invece il
    raggruppamento in ricette complete avviene qui in Python PRIMA di
    chiamare Gemini (deterministico, il file ha già la struttura necessaria),
    e si chunka per RICETTE complete, mai per righe. Il compito dell'AI si
    riduce a ripulire nomi, scegliere la categoria e trascrivere gli
    ingredienti — non a indovinare dove finisce una ricetta e ne inizia
    un'altra, né a risolvere l'ingrediente a un id di catalogo (quello è
    fuzzy matching deterministico lato frontend, vedi prodottoMatching.ts).

    prezzo_vendita_presente/tipo_prezzo_vendita: dichiarati dall'utente in
    fase di caricamento (stesso principio di tipo_prezzo per le materie
    prime — dichiarare, non far indovinare all'AI se il file ha o meno un
    prezzo di vendita, né se una colonna isolata sia netta o lorda). Se
    presente, il file deve avere una 5ª colonna col prezzo di vendita.
    """
    if tipo_prezzo_vendita not in _ISTRUZIONI_PREZZO_VENDITA:
        raise ValueError(f"tipo_prezzo_vendita non valido: '{tipo_prezzo_vendita}' (atteso: lordo, netto o entrambi).")
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(excel_file_bytes))
        else:
            # Stesso pattern di parse_vendite_excel_with_ai_stream: legge
            # tutti i fogli e sceglie quello con più righe, invece di
            # fidarsi ciecamente del primo (un file può avere fogli extra
            # non pertinenti, es. note o istruzioni).
            excel_file = pd.ExcelFile(io.BytesIO(excel_file_bytes))
            fogli = {nome: excel_file.parse(nome) for nome in excel_file.sheet_names}
            nome_foglio_scelto = max(fogli, key=lambda nome: len(fogli[nome]))
            df = fogli[nome_foglio_scelto]
    except Exception as e:
        logger.warning("Lettura file fallita: %s", e)
        raise ValueError("Impossibile leggere il file: formato non valido o file danneggiato.")

    if df.empty:
        raise ValueError("Il file non contiene righe da importare.")

    if len(df) > MAX_RIGHE_EXCEL:
        raise ValueError(
            f"Il file ha {len(df)} righe, oltre il limite di {MAX_RIGHE_EXCEL}: dividilo in più caricamenti."
        )

    # Le colonne attese (nome ricetta, categoria, ingrediente, quantità, ed
    # eventualmente prezzo di vendita) sono individuate per POSIZIONE, non
    # per nome esatto dell'header: l'utente ha chiesto esplicitamente un
    # approccio flessibile, e non c'è garanzia che l'intestazione sia
    # scritta esattamente come nel file di riferimento.
    colonne = list(df.columns)
    # "entrambi" richiede DUE colonne separate (netto e lordo), non una sola
    # da cui l'AI dovrebbe indovinare quale sia quale.
    n_colonne_prezzo = 2 if (prezzo_vendita_presente and tipo_prezzo_vendita == "entrambi") else (1 if prezzo_vendita_presente else 0)
    min_colonne = 4 + n_colonne_prezzo
    if len(colonne) < min_colonne:
        raise ValueError(
            f"Il file deve avere almeno {min_colonne} colonne: nome ricetta, categoria, ingrediente, quantità"
            + (", prezzo di vendita netto e lordo." if n_colonne_prezzo == 2
               else ", prezzo di vendita." if n_colonne_prezzo == 1 else ".")
        )
    col_nome, col_categoria, col_ingrediente, col_quantita = colonne[0], colonne[1], colonne[2], colonne[3]

    col_prezzo_netto = None
    col_prezzo_lordo = None
    if prezzo_vendita_presente:
        if tipo_prezzo_vendita == "entrambi":
            col_prezzo_netto, col_prezzo_lordo = colonne[4], colonne[5]
        elif tipo_prezzo_vendita == "netto":
            col_prezzo_netto = colonne[4]
        else:  # "lordo"
            col_prezzo_lordo = colonne[4]

    # ffill: alcuni export scrivono il nome ricetta/categoria/prezzo solo
    # sulla prima riga del gruppo (celle unite in Excel), lasciando le righe
    # successive vuote — senza questo perderebbero il collegamento alla
    # ricetta a cui appartengono.
    df[col_nome] = df[col_nome].ffill()
    df[col_categoria] = df[col_categoria].ffill()
    if col_prezzo_netto is not None:
        df[col_prezzo_netto] = df[col_prezzo_netto].ffill()
    if col_prezzo_lordo is not None:
        df[col_prezzo_lordo] = df[col_prezzo_lordo].ffill()

    # Raggruppamento per BLOCCO CONSECUTIVO, non un groupby per nome: due
    # ricette con lo stesso nome ma non adiacenti nel file restano due
    # ricette distinte, non vengono fuse in una sola.
    blocchi = []
    blocco_corrente = None
    for _, riga in df.iterrows():
        nome_ricetta = str(riga[col_nome]).strip() if pd.notna(riga[col_nome]) else ""
        if not nome_ricetta:
            continue
        if blocco_corrente is None or blocco_corrente["nome_ricetta"] != nome_ricetta:
            prezzo_netto_testo = None
            if col_prezzo_netto is not None and pd.notna(riga[col_prezzo_netto]):
                prezzo_netto_testo = str(riga[col_prezzo_netto]).strip()
            prezzo_lordo_testo = None
            if col_prezzo_lordo is not None and pd.notna(riga[col_prezzo_lordo]):
                prezzo_lordo_testo = str(riga[col_prezzo_lordo]).strip()
            blocco_corrente = {
                "nome_ricetta": nome_ricetta,
                "categoria": str(riga[col_categoria]).strip() if pd.notna(riga[col_categoria]) else "",
                "prezzo_netto_testo": prezzo_netto_testo,
                "prezzo_lordo_testo": prezzo_lordo_testo,
                "righe": [],
            }
            blocchi.append(blocco_corrente)
        ingrediente = str(riga[col_ingrediente]).strip() if pd.notna(riga[col_ingrediente]) else ""
        if not ingrediente:
            continue
        quantita_raw = riga[col_quantita]
        blocco_corrente["righe"].append({
            "ingrediente": ingrediente,
            "quantita_testo": str(quantita_raw) if pd.notna(quantita_raw) else "MANCANTE",
        })

    blocchi = [b for b in blocchi if b["righe"]]
    if not blocchi:
        raise ValueError("Non è stata trovata nessuna ricetta valida nel file.")

    if len(blocchi) > MAX_RICETTE_EXCEL:
        raise ValueError(
            f"Il file ha {len(blocchi)} ricette, oltre il limite di {MAX_RICETTE_EXCEL}: dividilo in più caricamenti."
        )

    cat_string = "\n".join([
        f"ID: {c.get('id')} - Nome: {c.get('nome_categoria')}"
        for c in categorie_disponibili
    ])

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    client = genai.Client(api_key=api_key)

    ricette_per_chunk = 15
    total_ricette = len(blocchi)
    total_chunks = (total_ricette + ricette_per_chunk - 1) // ricette_per_chunk
    sem = asyncio.Semaphore(5)

    istruzione_prezzo_vendita = (
        _ISTRUZIONI_PREZZO_VENDITA[tipo_prezzo_vendita] if prezzo_vendita_presente
        else "il file NON contiene un prezzo di vendita: lascia sempre 'prezzo_vendita_netto' e 'prezzo_vendita_lordo' a null."
    )

    # Markdown invece di un elenco a bullet: intestazione colonne scritta
    # una sola volta per ricetta (più economico del JSON, che ripeterebbe
    # le chiavi per ogni riga), e struttura a colonne esplicita che lascia
    # meno margine di interpretazione rispetto a un formato "nome: valore"
    # libero — i modelli sono addestrati massicciamente su tabelle
    # Markdown, è il formato tabellare de facto nei prompt.
    def _escape_pipe(testo) -> str:
        # Un "|" nel testo spezzerebbe le colonne della tabella.
        return str(testo).replace("|", "/")

    async def process_chunk(blocchi_chunk):
        testo_blocchi = []
        for i, b in enumerate(blocchi_chunk):
            # Colonna "Riga" (solo per Gemini, non fa parte dei dati): con
            # ricette che hanno lo stesso ingrediente ripetuto più volte
            # (quantità diverse), abbiamo verificato che l'AI a volte le
            # tratta come "duplicati" e le unisce in una sola riga, nonostante
            # l'istruzione esplicita di non farlo — un numero univoco per
            # riga rende ogni riga visibilmente distinta anche quando il
            # nome ingrediente è identico, disincentivando la deduplica alla
            # radice invece di fare leva solo sul testo dell'istruzione.
            righe_tabella = "\n".join(
                f"| {j + 1} | {_escape_pipe(r['ingrediente'])} | {_escape_pipe(r['quantita_testo'])} |"
                for j, r in enumerate(b["righe"])
            )
            meta_bits = [f"- Categoria nel file: {b['categoria'] or 'non indicata'}"]
            if b.get("prezzo_netto_testo"):
                meta_bits.append(f"- Prezzo vendita netto nel file: {b['prezzo_netto_testo']}")
            if b.get("prezzo_lordo_testo"):
                meta_bits.append(f"- Prezzo vendita lordo nel file: {b['prezzo_lordo_testo']}")
            meta_testo = "\n".join(meta_bits)
            testo_blocchi.append(
                f"### RICETTA (id_blocco={i}): {b['nome_ricetta']}\n"
                f"{meta_testo}\n\n"
                f"| Riga | Ingrediente | Quantità |\n"
                f"|---|---|---|\n"
                f"{righe_tabella}"
            )
        testo = "\n\n".join(testo_blocchi)

        prompt = f"""
Sei un assistente esperto in ristorazione in Italia.
Ti fornisco un elenco di ricette raggruppate, ciascuna con i propri ingredienti e quantità in un file EXCEL o CSV, formattate in Markdown (intestazione ### per ricetta, tabella per gli ingredienti).

Analizza ogni ricetta rigorosamente RIGA per RIGA della tabella e restituisci un JSON che rispetti questo schema:
0. 'id_blocco': RIPORTA esattamente il numero indicato tra parentesi "id_blocco=" per quella ricetta, senza modificarlo, calcolarlo o dedurlo — è un riferimento interno, non fa parte del nome né della ricetta stessa.
1. 'nome_ricetta': il nome della ricetta, ripulito da spazi/refusi evidenti ma SENZA cambiarne il significato.
2. 'id_categoria': scegli l'ID della categoria più adatta tra questa lista, usando anche la categoria indicata nel file come indizio. Se nessuna si adatta con sicurezza, imposta null — non indovinare.
3. 'prezzo_vendita_netto' e 'prezzo_vendita_lordo': {istruzione_prezzo_vendita}
4. 'ingredienti': un elenco con, per ciascuna riga della tabella "Riga | Ingrediente | Quantità" fornita (la colonna "Riga" è solo un numero di riferimento per distinguere righe con lo stesso ingrediente: non fa parte del nome, non includerla nel JSON):
   - 'nome_ingrediente_estratto': il nome dell'ingrediente (colonna "Ingrediente") ripulito da spazi superflui, MA conservane il testo originale (incluse eventuali indicazioni di formato/confezione come "5kg" o "6x1kg") — servirà per abbinarlo a un catalogo, quindi non semplificarlo né accorciarlo.
   - 'quantita': COPIA il numero della colonna "Quantità" esattamente come scritto per quella riga, cifra per cifra — non calcolarlo, non stimarlo, non arrotondarlo, non sostituirlo con un valore tipico che conosci per quella ricetta. Se è "MANCANTE", imposta 0.
TASSATIVO: non saltare nessuna ricetta e nessuna riga della tabella tra quelle fornite, non inventarne di nuove, non unire ricette diverse. Restituisci gli ingredienti di ogni ricetta nello STESSO ORDINE (stessa sequenza di "Riga") della tabella. Se lo STESSO nome ingrediente compare su più righe della stessa ricetta (numeri di "Riga" diversi, anche con quantità diverse o quasi identiche), sono RIGHE DIVERSE: NON deduplicare, NON unirle in una sola — restituisci un elemento separato per OGNI numero di "Riga", esattamente come faresti se i nomi ingrediente fossero diversi tra loro. Il numero di elementi in 'ingredienti' DEVE essere identico al numero di righe della tabella di quella ricetta, sempre, senza eccezioni.

Ecco un esempio di dati presente nel file Excel (con id_blocco=5 come sarebbe indicato nella sezione "Ricette da elaborare" qui sotto):
### RICETTA (id_blocco=5): AGRUMI
- Categoria nel file: SORBETTI

| Riga | Ingrediente | Quantità |
|---|---|---|
| 1 | acqua | 70,80 |
| 2 | joybase delymix 50 6x1kg | 241,22 |
| 3 | joyplus prosoft 6kg (6x1kg) | 643,73 |

Il json finale che deve restituire questo esempio di ricetta è:
{{
  "ricette": [
    {{
      "id_blocco": 5,
      "nome_ricetta": "AGRUMI",
      "id_categoria": 123,
        "prezzo_vendita_netto": 0,
        "prezzo_vendita_lordo": 0,
        "ingredienti": [
            {{
                "nome_ingrediente_estratto": "acqua",
                "quantita": 70.80
            }},
            {{
                "nome_ingrediente_estratto": "joybase delymix 50 6x1kg",
                "quantita": 241.22
            }},
            {{
                "nome_ingrediente_estratto": "joyplus prosoft 6kg (6x1kg)",
                "quantita": 643.73
            }}
        ]
    }}
  ]
}}

Lista Categorie Disponibili:
{cat_string}

Ricette da elaborare:
{testo}

Ritorna ESCLUSIVAMENTE un JSON valido seguendo lo schema richiesto.
"""

        # Log temporaneo su richiesta esplicita (debug del prompt appena
        # modificato): print(), non logger, perché non esiste ancora nessuna
        # configurazione di logging nel progetto (nessun basicConfig/handler
        # da nessuna parte) — un logger.info() qui non comparirebbe affatto
        # nel terminale. Da togliere quando non serve più.
        print(f"\n{'=' * 80}\n[RICETTE] PROMPT INVIATO A GEMINI:\n{'=' * 80}\n{prompt}\n{'=' * 80}\n")

        # gemini-2.5-flash, non un modello "pro": il raggruppamento delle
        # righe in ricette (la parte davvero non banale) è già fatto in
        # Python PRIMA di arrivare qui (vedi sopra) — il compito che resta
        # all'AI (ripulire nomi, scegliere una categoria da una lista data,
        # trascrivere ingredienti/quantità) è lo stesso genere di estrazione
        # strutturata già affidata a flash per materie prime/vendite. Un
        # modello "pro" qui è stato provato e scartato: più lento (l'utente
        # ha segnalato tempi di attesa concreti) e più soggetto a 503 "alta
        # domanda" essendo in preview, senza un reale bisogno di ragionamento
        # più sofisticato per questo compito ormai già scomposto.
        max_retries = 5
        chunk_result = None
        last_error = None

        async with sem:
            for attempt in range(max_retries):
                try:
                    response = await client.aio.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=ParsedRicetteResult,
                            temperature=0.1,
                            http_options=types.HttpOptions(timeout=_TIMEOUT_TESTO_MS),
                        ),
                    )
                    chunk_result = response.text
                    print(f"\n{'=' * 80}\n[RICETTE] RISPOSTA GEMINI (tentativo {attempt + 1}):\n{'=' * 80}\n{chunk_result}\n{'=' * 80}\n")
                    break
                except Exception as e:
                    last_error = e
                    print(f"\n[RICETTE] ERRORE al tentativo {attempt + 1}: {e}\n")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue

        nomi_blocco = ", ".join(b["nome_ricetta"] for b in blocchi_chunk)
        if chunk_result is None:
            logger.error("Import ricette '%s': %s", nomi_blocco, last_error)
            return {"ricette": [], "errore": f"Ricette '{nomi_blocco}': il servizio AI non ha risposto correttamente."}

        try:
            parsed_chunk = json.loads(chunk_result)
        except Exception as e:
            logger.error("Import ricette '%s': risposta AI non interpretabile: %s", nomi_blocco, e)
            return {"ricette": [], "errore": f"Ricette '{nomi_blocco}': risposta AI non interpretabile."}

        ricette_estratte = parsed_chunk.get("ricette", [])
        _correggi_quantita_con_originali(ricette_estratte, blocchi_chunk)
        return {"ricette": ricette_estratte, "errore": None}

    chunks_di_blocchi = [blocchi[i:i + ricette_per_chunk] for i in range(0, total_ricette, ricette_per_chunk)]
    tasks = [process_chunk(c) for c in chunks_di_blocchi]

    all_ricette = []
    blocchi_falliti = []
    completed_chunks = 0
    for future in asyncio.as_completed(tasks):
        esito = await future
        all_ricette.extend(esito["ricette"])
        if esito["errore"]:
            logger.warning("parse_ricette_excel_with_ai_stream: blocco fallito - %s", esito["errore"])
            blocchi_falliti.append(esito["errore"])
        completed_chunks += 1

        progress_pct = int((completed_chunks / total_chunks) * 100)
        yield json.dumps({"progress": progress_pct}) + "\n"

    if blocchi_falliti:
        # Stesso principio "tutto o niente" degli altri due importatori: mai
        # consegnare ricette a metà.
        raise ValueError("Impossibile analizzare l'intero file: " + "; ".join(blocchi_falliti))

    result_payload = {"ricette": all_ricette}
    yield json.dumps({"result": result_payload}) + "\n"
