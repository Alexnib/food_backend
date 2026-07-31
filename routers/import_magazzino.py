from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import StreamingResponse
from database.config import Database
from utils.auth_utils import get_user_sede
from utils.ai_parser import parse_excel_with_ai_stream, parse_fattura_with_ai, MAX_FILE_FATTURA
from utils.ai_usage import check_and_log_ai_usage
from typing import List, Optional
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
    check_and_log_ai_usage(id_sede, "import_excel_materie_prime")

    # 1. Ottieni le categorie
    cat_res = supabase.table("categoria_prodotti").select("*").eq("id_sede", id_sede).execute()
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
            yield json.dumps({"error": str(e)}) + "\n"

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

    check_and_log_ai_usage(auth_data["id_sede"], "import_fattura")

    file_parts = []
    for f in files:
        content = await f.read()
        mime_type = f.content_type or "application/pdf"
        file_parts.append((content, mime_type))

    try:
        json_str = parse_fattura_with_ai(file_parts)
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
    
    articoli_to_insert = []
    costi_to_insert = []
    
    mesi_ita = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    now = datetime.datetime.now()
    mese_corrente = mesi_ita[now.month - 1]
    anno_corrente = now.year

    for item in request.prodotti:
        # Trova id_iva
        id_iva = next((i["id"] for i in iva_list if i["iva"] == item.iva_perc), iva_list[0]["id"] if iva_list else None)
        
        # Fornitore estratto dalla fattura, se presente (l'import da Excel non
        # lo fornisce mai: resta "Sconosciuto" come già in precedenza).
        fornitore = item.fornitore or "Sconosciuto"

        if item.tipo == "Materia Prima":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "id_iva_acquisto": id_iva,
                # Per una materia prima pura il frontend non chiede la
                # categoria (è legata solo a rivendita/costi): item.id_categoria
                # arriva quindi None, salvato così com'è.
                "id_categoria_prodotto": item.id_categoria,
                "fornitore": fornitore,
                "anno": anno_corrente,
                "is_materia_prima": True,
                "is_rivendita": False
            })
        elif item.tipo == "Entrambi":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "prezzo_vendita_netto": 0.0,
                "prezzo_vendita_lordo": 0.0,
                "margine": 0.0,
                "margine_perc": 0.0,
                "id_iva_acquisto": id_iva,
                "id_iva_rivendita": id_iva,
                "id_categoria_prodotto": item.id_categoria,
                "is_materia_prima": True,
                "is_rivendita": True,
                "fornitore": fornitore,
                "anno": anno_corrente
            })
        elif item.tipo == "Rivendita":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "prezzo_vendita_netto": 0.0,
                "prezzo_vendita_lordo": 0.0,
                "margine": 0.0,
                "margine_perc": 0.0,
                "id_iva_acquisto": id_iva,
                "id_iva_rivendita": id_iva,
                "id_categoria_prodotto": item.id_categoria,
                "is_materia_prima": False,
                "is_rivendita": True,
                "fornitore": fornitore,
                "anno": anno_corrente
            })
        elif item.tipo == "Costo":
            costi_to_insert.append({
                "id_sede": id_sede,
                "id_categoria": item.id_categoria,
                "note": item.nome_prodotto,
                "importo": item.costo_lordo,
                "mese": mese_corrente,
                "anno": anno_corrente,
                "anno_mese": now.strftime("%Y-%m")
            })
            
    try:
        # sql/014_import_magazzino_save_rpc.sql: le due insert (articoli,
        # costi_anno_mese) girano in un'unica transazione, così un fallimento
        # a metà non lascia più articoli orfani salvati senza il relativo
        # costo. Se la funzione non esiste ancora sul DB (script non ancora
        # eseguito) degradiamo alle due insert separate di prima — a
        # differenza della race condition di registra_vendite_bulk, qui non
        # c'è un bug attivo da bloccare: il comportamento "vecchio" resta
        # corretto, solo non atomico, quindi un fallback silenzioso è sicuro.
        try:
            supabase.rpc("save_import_articoli_costi", {
                "p_articoli": articoli_to_insert,
                "p_costi": costi_to_insert,
            }).execute()
        except Exception as e:
            if getattr(e, "code", None) != "PGRST202":  # "function not found" (PostgREST)
                raise
            logger.info("save_import_articoli_costi non ancora presente sul DB (sql/014 non eseguito): fallback a insert separate")
            if articoli_to_insert:
                supabase.table("articoli").insert(articoli_to_insert).execute()
            if costi_to_insert:
                supabase.table("costi_anno_mese").insert(costi_to_insert).execute()
    except Exception as e:
        # Qui l'errore più comune è un vincolo DB violato (es. id_categoria
        # non valido) — genuinamente riconducibile ai dati, non un bug: resta
        # un 400 col messaggio originale, ma logghiamo comunque lo stack per
        # poter distinguere in seguito un vincolo violato da un bug reale.
        logger.exception("save_imported_products: errore nel salvataggio")
        raise HTTPException(status_code=400, detail=f"Errore nel salvataggio: {str(e)}")

    return {"message": "Importazione completata con successo", "inseriti_articoli": len(articoli_to_insert), "inseriti_costi": len(costi_to_insert)}
