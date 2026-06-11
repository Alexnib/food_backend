from fastapi import APIRouter, HTTPException, status, Depends
from models.management import CreateNegozio, CreateSede
from database.config import Database
from uuid import UUID
from utils.auth_utils import get_current_user

router = APIRouter(
    prefix="/api/admin",
    tags=["Gestione Struttura"]
)

supabase = Database.get_client()

@router.post("/negozi", status_code=status.HTTP_201_CREATED)
async def create_negozio(data: CreateNegozio, current_user = Depends(get_current_user)):
    """Crea l'entità principale del Negozio (Brand)"""
    try:
        res = supabase.table("negozi").insert(data.model_dump(mode="json")).execute()
        return {"message": "Negozio creato correttamente", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore creazione negozio: {str(e)}")
    
@router.post("/sedi", status_code=status.HTTP_201_CREATED)
async def create_sede(data: CreateSede, current_user = Depends(get_current_user)):
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
async def lista_sedi(id_negozio: UUID, current_user = Depends(get_current_user)):
    """Recupera tutte le sedi con i dati del negozio associato (Join)"""
    try:
        res = supabase.from_("sedi").select("*, negozi(*)").eq("id_negozio", str(id_negozio)).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))