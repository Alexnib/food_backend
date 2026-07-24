from fastapi import APIRouter, HTTPException, status, Depends
from models.management import CreateNegozio, CreateSede, UpdateMiaSede
from database.config import Database
from uuid import UUID
from utils.auth_utils import get_current_user, get_user_sede

router = APIRouter(
    prefix="/api/admin",
    tags=["Gestione Struttura"]
)

supabase = Database.get_client()

@router.post("/negozi", status_code=status.HTTP_201_CREATED)
def create_negozio(data: CreateNegozio, current_user = Depends(get_current_user)):
    """Crea l'entità principale del Negozio (Brand)"""
    try:
        res = supabase.table("negozi").insert(data.model_dump(mode="json")).execute()
        return {"message": "Negozio creato correttamente", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore creazione negozio: {str(e)}")
    
@router.post("/sedi", status_code=status.HTTP_201_CREATED)
def create_sede(data: CreateSede, current_user = Depends(get_current_user)):
    """Crea una sede fisica legata a un negozio specifico"""
    try:
        sede_res = supabase.table("sedi").insert(data.model_dump(mode="json")).execute()
        nuova_sede = sede_res.data[0]
        id_nuova_sede = nuova_sede["id"]

        # STEP B: Aggiorniamo l'utente (current_user.id viene dal Token JWT)
        supabase.table("users").update({
            "id_sede": id_nuova_sede
        }).eq("id", current_user.id).execute()

        return {
            "message": "Sede creata e profilo utente aggiornato!",
            "sede": nuova_sede
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore: {str(e)}")
    
@router.get("/sedi", status_code=status.HTTP_200_OK)
def lista_sedi(id_negozio: UUID, current_user = Depends(get_current_user)):
    """Recupera tutte le sedi con i dati del negozio associato (Join)"""
    try:
        res = supabase.from_("sedi").select("*, negozi(*)").eq("id_negozio", str(id_negozio)).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sedi/me")
def get_mia_sede(auth_data=Depends(get_user_sede)):
    """La sede (e il negozio associato) dell'utente corrente, per precompilare
    il form di modifica nel profilo."""
    res = supabase.table("sedi").select("*, negozi(*)").eq("id", auth_data["id_sede"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Sede non trovata.")
    return res.data[0]


@router.put("/sedi/me")
def update_mia_sede(data: UpdateMiaSede, auth_data=Depends(get_user_sede)):
    """
    Modifica i dati della propria sede/negozio (quelli impostati in
    onboarding): mai un id arbitrario dal client, sempre e solo la sede
    dell'utente che chiama.
    """
    sede_fields = {k: v for k, v in {
        "indirizzo": data.indirizzo, "comune": data.comune, "civico": data.civico,
    }.items() if v is not None}
    negozio_fields = {k: v for k, v in {
        "nome_negozio": data.nome_negozio, "partita_iva": data.partita_iva,
    }.items() if v is not None}

    if sede_fields:
        supabase.table("sedi").update(sede_fields).eq("id", auth_data["id_sede"]).execute()

    if negozio_fields:
        sede_row = supabase.table("sedi").select("id_negozio").eq("id", auth_data["id_sede"]).execute()
        id_negozio = sede_row.data[0].get("id_negozio") if sede_row.data else None
        if id_negozio:
            supabase.table("negozi").update(negozio_fields).eq("id", id_negozio).execute()

    res = supabase.table("sedi").select("*, negozi(*)").eq("id", auth_data["id_sede"]).execute()
    return res.data[0] if res.data else None