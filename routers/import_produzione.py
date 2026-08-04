from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import StreamingResponse
from database.config import Database
from utils.auth_utils import get_user_sede
from utils.ai_parser import parse_ricette_excel_with_ai_stream
from utils.ai_usage import check_and_log_ai_usage
from routers.produzione import _calcola_ingredienti_e_costo
import json
import logging
from models.produzione import SaveImportRicetteRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/produzione/import", tags=["Importazione Ricette"])
supabase = Database.get_client()


@router.post("/upload")
async def upload_excel_for_import_ricette(
    file: UploadFile = File(...),
    # Dichiarati dall'utente in fase di caricamento (vedi ImportRicetteExcelModal.tsx):
    # se il file contiene un prezzo di vendita e, se sì, come è espresso —
    # stesso principio di tipo_prezzo per le materie prime.
    prezzo_vendita_presente: bool = Form(False),
    tipo_prezzo_vendita: str = Form("entrambi"),
    auth_data = Depends(get_user_sede),
):
    """
    Riceve il file Excel/CSV delle ricette e lo invia a Gemini per
    l'estrazione. Ritorna uno stream NDJSON per aggiornamenti di progresso
    progressivi e il risultato finale — stesso schema di /api/import/upload
    (materie prime) e /api/vendite/import/upload.
    """
    id_sede = auth_data["id_sede"]
    check_and_log_ai_usage(id_sede, "import_excel_ricette")

    # Solo le categorie ricette (id_macro_categoria = 2), stesso filtro di
    # GET /api/magazzino/categorie/ricette: le ricette importate vanno
    # abbinate alle stesse categorie di quelle create a mano, non a
    # qualunque categoria della sede.
    cat_res = supabase.table("categoria_prodotti").select("*").eq("id_sede", id_sede).eq("id_macro_categoria", 2).execute()
    categorie = cat_res.data or []

    content = await file.read()
    filename = file.filename

    async def event_generator():
        try:
            async for chunk in parse_ricette_excel_with_ai_stream(
                content, filename, categorie, prezzo_vendita_presente, tipo_prezzo_vendita
            ):
                yield chunk
        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    # Content-Encoding esplicito: disattiva il GZipMiddleware globale (vedi
    # main.py) su questa risposta, altrimenti Starlette bufferizza gli eventi
    # NDJSON e la barra di progresso resta bloccata a zero (stesso bug già
    # risolto per gli altri due importatori — vedi import_magazzino.py).
    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={"Content-Encoding": "identity"},
    )


@router.post("/save")
def save_imported_ricette(request: SaveImportRicetteRequest, auth_data = Depends(get_user_sede)):
    id_sede = auth_data["id_sede"]

    if not request.ricette:
        return {"message": "Nessuna ricetta da salvare", "inserite": 0}

    payload = [r.model_dump() for r in request.ricette]

    try:
        # save_import_ricette (sql/016): crea tutte le ricette e i relativi
        # ingredienti in un'unica transazione, calcolando il food cost con la
        # stessa formula di _calcola_ingredienti_e_costo — un fallimento a
        # metà non deve lasciare ricette orfane senza ingredienti.
        res = supabase.rpc("save_import_ricette", {
            "p_id_sede": id_sede,
            "p_ricette": payload,
        }).execute()
        return {"message": "Importazione completata con successo", "inserite": len(res.data or [])}
    except Exception as e:
        if getattr(e, "code", None) != "PGRST202":  # "function not found" (PostgREST)
            logger.exception("save_imported_ricette: errore nel salvataggio")
            raise HTTPException(status_code=400, detail=f"Errore nel salvataggio: {str(e)}")

    # Fallback: sql/016 non ancora eseguita sul DB. Stessa logica di
    # create_ricetta (routers/produzione.py), ripetuta per ogni ricetta:
    # funziona, solo senza la garanzia di atomicità dell'RPC.
    logger.info("save_import_ricette non ancora presente sul DB (sql/016 non eseguito): fallback a insert singole")
    inserite = 0
    try:
        for ricetta in request.ricette:
            ricetta_insert = {
                "nome_ricetta": ricetta.nome_ricetta,
                "descrizione_ricetta": ricetta.descrizione_ricetta,
                "id_categoria_prodotto": ricetta.id_categoria_prodotto,
                "id_sede": id_sede,
                "costo_ricetta_reale": 0.0,
                "prezzo_vendita_lordo": ricetta.prezzo_vendita_lordo,
                "prezzo_vendita_netto": ricetta.prezzo_vendita_netto,
                "id_iva_vendita": ricetta.id_iva_vendita,
            }
            res_ricetta = supabase.table("ricette").insert(ricetta_insert).execute()
            id_ricetta_creata = res_ricetta.data[0]["id"]

            ingredienti_da_inserire, costo_totale_ricetta = _calcola_ingredienti_e_costo(
                id_sede, id_ricetta_creata, ricetta.ingredienti
            )
            if ingredienti_da_inserire:
                supabase.table("ingredienti_ricetta").insert(ingredienti_da_inserire).execute()

            supabase.table("ricette").update({"costo_ricetta_reale": round(costo_totale_ricetta, 2)}).eq("id", id_ricetta_creata).execute()
            inserite += 1
    except Exception as e:
        logger.exception("save_imported_ricette: errore nel salvataggio (fallback)")
        raise HTTPException(status_code=400, detail=f"Errore nel salvataggio (ricetta {inserite + 1} di {len(request.ricette)}): {str(e)}")

    return {"message": "Importazione completata con successo", "inserite": inserite}
