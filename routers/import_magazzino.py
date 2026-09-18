from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import StreamingResponse
from database.config import Database
from utils.auth_utils import get_user_sede
from utils.errors import errore_http, messaggio_pubblico
from utils.ai_parser import parse_excel_with_ai_stream, parse_fattura_with_ai, MAX_FILE_FATTURA
from utils.ai_usage import check_and_log_ai_usage
from utils.articoli_match import chiave_articolo, data_prezzo_valida
from utils.db_fetch import fetch_all_parallel
from routers.magazzino import calcola_margini
from routers.produzione import ricalcola_costo_ricette
from typing import List, Optional
import asyncio
import json
import datetime
import logging
from models.magazzino import ImportItem, SaveImportRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["Importazione Massiva"])
supabase = Database.get_client()

@router.post("/upload")
async def upload_excel_for_import(
    file: UploadFile = File(...),
    # Dichiarato dall'utente in fase di caricamento (vedi ImportazioneExcelModal.tsx):
    # cosa contiene la colonna/e prezzo del file, per non far indovinare
    # all'AI se un prezzo isolato sia netto o lordo. "entrambi" di default
    # per compatibilità con chi chiama questo endpoint senza specificarlo.
    tipo_prezzo: str = Form("entrambi"),
    auth_data = Depends(get_user_sede),
):
    id_sede = auth_data["id_sede"]
    # check_and_log_ai_usage e la select categorie sono chiamate sincrone di
    # supabase-py: dentro questo endpoint async bloccherebbero l'event loop
    # (e con esso tutte le richieste concorrenti del server) per la loro
    # durata — asyncio.to_thread le sposta su un thread del pool.
    await asyncio.to_thread(check_and_log_ai_usage, id_sede, "import_excel_materie_prime")

    # 1. Ottieni le categorie
    cat_res = await asyncio.to_thread(
        lambda: supabase.table("categoria_prodotti").select("*").eq("id_sede", id_sede).execute()
    )
    categorie = cat_res.data or []

    # 2. Leggi il file
    content = await file.read()
    filename = file.filename

    # 3. Manda a Gemini — parse_excel_with_ai_stream elabora i blocchi in
    # parallelo (vedi utils/ai_parser.py) e restituisce uno stream NDJSON con
    # il progresso via via che ogni blocco finisce, stesso schema di
    # /api/vendite/import/upload: un errore di validazione nostro (es.
    # limite righe superato) o un errore AI arrivano entrambi come riga
    # {"error": ...} nello stream, non più come HTTPException, perché gli
    # header della risposta sono già stati inviati al client a quel punto.
    async def event_generator():
        try:
            async for chunk in parse_excel_with_ai_stream(content, filename, categorie, tipo_prezzo):
                yield chunk
        except Exception as e:
            yield json.dumps({"error": messaggio_pubblico(e, "upload_excel_for_import", "Errore durante l'analisi del file. Riprova.")}) + "\n"

    # Content-Encoding esplicito per disattivare il GZipMiddleware globale
    # (vedi main.py) su questa risposta: senza questo, Starlette bufferizza
    # ogni chunk dentro il proprio compressore zlib e non lo inoltra davvero
    # al client finché il buffer non supera "minimum_size" o lo stream non si
    # chiude — per una StreamingResponse fatta di tante righe minuscole come
    # questa, significa che TUTTI gli eventi di progresso arrivano insieme
    # solo alla fine, azzerando la barra di avanzamento lato frontend anche
    # se il backend li genera correttamente uno alla volta. Verificato con un
    # test isolato: identico stream, senza questo header tutti gli eventi
    # arrivavano nello stesso istante finale, con questo header arrivavano
    # scaglionati nel tempo come generati.
    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={"Content-Encoding": "identity"},
    )

@router.post("/upload-fattura")
async def upload_fattura_for_import(files: List[UploadFile] = File(...), auth_data = Depends(get_user_sede)):
    """
    Estrae fornitore, partita IVA e le righe prodotto da una o più fatture di
    acquisto (PDF o foto). A differenza di /upload (Excel), qui l'utente
    sceglie sempre a mano tipo/categoria nella schermata di conferma: vedi
    utils.ai_parser.parse_fattura_with_ai per il perché.
    """
    if len(files) > MAX_FILE_FATTURA:
        raise HTTPException(status_code=400, detail=f"Troppi file in un solo caricamento ({len(files)}): il limite è {MAX_FILE_FATTURA}.")

    await asyncio.to_thread(check_and_log_ai_usage, auth_data["id_sede"], "import_fattura")

    file_parts = []
    for f in files:
        content = await f.read()
        mime_type = f.content_type or "application/pdf"
        file_parts.append((content, mime_type))

    try:
        # parse_fattura_with_ai è sincrona e usa il client Gemini sincrono
        # (fino a 3 tentativi x 60s di timeout ciascuno, vedi ai_parser.py):
        # senza asyncio.to_thread, un'analisi lenta blocca l'event loop del
        # server per l'intera durata, non solo questa richiesta.
        json_str = await asyncio.to_thread(parse_fattura_with_ai, file_parts)
        return json.loads(json_str)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("upload_fattura_for_import: errore inatteso")
        raise HTTPException(status_code=500, detail="Errore interno durante l'analisi delle fatture. Riprova più tardi.")

@router.post("/save")
def save_imported_products(request: SaveImportRequest, auth_data = Depends(get_user_sede)):
    id_sede = auth_data["id_sede"]

    # Ottieni la tabella IVA per fare il match
    iva_res = supabase.table("iva").select("*").execute()
    iva_list = iva_res.data or []

    mesi_ita = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    now = datetime.datetime.now()
    mese_corrente = mesi_ita[now.month - 1]
    anno_corrente = now.year

    tipi_articolo = ("Materia Prima", "Entrambi", "Rivendita")

    # Articoli già a catalogo, per riconoscere quelli reimportati: prima di
    # questa modifica l'import inseriva SEMPRE una riga nuova, quindi ogni
    # reimport dello stesso prodotto lo duplicava. Ora un articolo con lo
    # stesso nome (e la stessa unità di misura) viene aggiornato al nuovo
    # prezzo, e il prezzo precedente resta nello storico (trigger su
    # articoli, vedi sql/020). Se per lo stesso nome esistono già più copie
    # (duplicati storici) le aggiorniamo tutte, così il food cost delle
    # ricette resta corretto qualunque copia usino.
    esistenti_per_chiave = {}
    if any(item.tipo in tipi_articolo for item in request.prodotti):
        def make_query(with_count):
            return supabase.table("articoli").select(
                "id, nome_articolo, unita_misura, is_rivendita, prezzo_vendita_netto",
                count="exact" if with_count else None,
            ).eq("id_sede", id_sede).eq("is_cancelled", False)

        for articolo in fetch_all_parallel(make_query):
            chiave = chiave_articolo(articolo["nome_articolo"], articolo["unita_misura"])
            esistenti_per_chiave.setdefault(chiave, []).append(articolo)

    # In ordine di data: se la stessa riga compare più volte (più fatture, o
    # lo stesso prodotto ripetuto) l'ultimo prezzo cronologico è quello che
    # resta sull'articolo, e tutti finiscono nello storico.
    righe = [(data_prezzo_valida(item.data_prezzo), idx, item) for idx, item in enumerate(request.prodotti)]
    righe.sort(key=lambda r: (r[0], r[1]))

    articoli_ops = []
    costi_to_insert = []
    slot_per_chiave = {}  # articoli nuovi creati in questo stesso import

    for data_prezzo, _, item in righe:
        if item.tipo == "Costo":
            costi_to_insert.append({
                "id_sede": id_sede,
                "id_categoria": item.id_categoria,
                "note": item.nome_prodotto,
                "importo": item.costo_lordo,
                "mese": mese_corrente,
                "anno": anno_corrente,
                "anno_mese": now.strftime("%Y-%m")
            })
            continue

        if item.tipo not in tipi_articolo:
            continue

        # Trova id_iva
        id_iva = next((i["id"] for i in iva_list if i["iva"] == item.iva_perc), iva_list[0]["id"] if iva_list else None)

        prezzi = {
            "prezzo_acquisto_netto": item.costo_netto,
            "prezzo_acquisto_lordo": item.costo_lordo,
            "id_iva_acquisto": id_iva,
            "anno": data_prezzo.year,
            "data_prezzo": data_prezzo.isoformat(),
            "fonte": request.fonte,
        }
        # Fornitore estratto dalla fattura, se presente (l'import da Excel non
        # lo fornisce mai): su un articolo esistente non sovrascriviamo un
        # fornitore già noto con un valore vuoto.
        fornitore_aggiornamento = {"fornitore": item.fornitore} if item.fornitore else {}

        chiave = chiave_articolo(item.nome_prodotto, item.unita_misura)
        esistenti = esistenti_per_chiave.get(chiave)

        if esistenti:
            for articolo in esistenti:
                op = {"op": "update", "id": articolo["id"], **prezzi, **fornitore_aggiornamento}
                if articolo.get("is_rivendita"):
                    # Come update_articolo: il margine dipende dal costo d'acquisto.
                    margine, margine_perc = calcola_margini(articolo.get("prezzo_vendita_netto") or 0, item.costo_netto)
                    op["margine"] = margine
                    op["margine_perc"] = margine_perc
                articoli_ops.append(op)
        elif chiave in slot_per_chiave:
            articoli_ops.append({"op": "update", "slot": slot_per_chiave[chiave], **prezzi, **fornitore_aggiornamento})
        else:
            slot_per_chiave[chiave] = len(slot_per_chiave)
            nuovo = {
                "op": "insert",
                "slot": slot_per_chiave[chiave],
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                # Per una materia prima pura il frontend non chiede la
                # categoria (è legata solo a rivendita/costi): item.id_categoria
                # arriva quindi None, salvato così com'è.
                "id_categoria_prodotto": item.id_categoria,
                "fornitore": item.fornitore or "Sconosciuto",
                "is_materia_prima": item.tipo in ("Materia Prima", "Entrambi"),
                "is_rivendita": item.tipo in ("Rivendita", "Entrambi"),
                **prezzi,
            }
            if item.tipo in ("Rivendita", "Entrambi"):
                nuovo["id_iva_rivendita"] = id_iva
            articoli_ops.append(nuovo)

    try:
        # sql/020: match/aggiornamento/inserimento articoli + costi in un'unica
        # transazione (come faceva save_import_articoli_costi), con lo storico
        # prezzi scritto dal trigger su articoli. Niente fallback silenzioso se
        # la funzione manca: tornerebbe ai duplicati che questa modifica evita.
        res = supabase.rpc("import_articoli_con_storico", {
            "p_id_sede": id_sede,
            "p_articoli": articoli_ops,
            "p_costi": costi_to_insert,
        }).execute()
        risultato = res.data if isinstance(res.data, dict) else {}
    except Exception as e:
        # Qui l'errore più comune è un vincolo DB violato (es. id_categoria
        # non valido) — genuinamente riconducibile ai dati, non un bug: resta
        # un 400 col messaggio originale, ma logghiamo comunque lo stack per
        # poter distinguere in seguito un vincolo violato da un bug reale.
        logger.exception("save_imported_products: errore nel salvataggio")
        raise errore_http(e, 'save_imported_products', "Errore durante il salvataggio dell'importazione.", 400, per_codice={'23503': 'Una categoria selezionata non esiste più. Ricarica la pagina e riprova.'})

    # Il prezzo di acquisto di alcuni articoli è cambiato: il food cost delle
    # ricette che li usano come ingrediente è obsoleto (stesso motivo di
    # update_articolo). Il salvataggio è già avvenuto: un problema qui non
    # deve far fallire l'import, solo essere loggato.
    ids_cambiati = risultato.get("ids_prezzo_cambiato") or []
    if ids_cambiati:
        try:
            id_ricette = set()
            for i in range(0, len(ids_cambiati), 100):
                affected = supabase.table("ingredienti_ricetta").select("id_ricetta").in_("id_materia_prima", ids_cambiati[i:i + 100]).execute()
                id_ricette.update(r["id_ricetta"] for r in (affected.data or []))
            ricalcola_costo_ricette(list(id_ricette))
        except Exception:
            logger.exception("save_imported_products: ricalcolo food cost ricette fallito dopo l'import")

    return {
        "message": "Importazione completata con successo",
        "inseriti_articoli": risultato.get("inseriti", 0),
        "aggiornati_articoli": risultato.get("aggiornati", 0),
        "solo_storico": risultato.get("solo_storico", 0),
        "inseriti_costi": risultato.get("costi", 0),
    }
