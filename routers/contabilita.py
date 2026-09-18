from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.contabilita import CategoriaCostoCreate, CostoAnnoMeseCreate, CategoriaCostoUpdate, CostoAnnoMeseUpdate
from utils.auth_utils import get_user_sede
from utils.errors import errore_http

router = APIRouter(prefix="/api/contabilita", tags=["Contabilità"])
supabase = Database.get_client()

MAPPATURA_MESI = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12"
}

@router.post("/categorie", status_code=status.HTTP_201_CREATED)
def create_categoria(data: CategoriaCostoCreate, auth_data = Depends(get_user_sede)):
    try:
        insert_data = data.model_dump(mode="json")
        insert_data["id_sede"] = auth_data["id_sede"]

        res = supabase.table("categorie_costi").insert(insert_data).execute()
        return {"message": "Categoria creata", "data": res.data[0]}
    except Exception as e:
        raise errore_http(e, 'create_categoria', 'Errore durante la creazione della categoria.', 400)

@router.get("/categorie", status_code=status.HTTP_200_OK)
def get_categorie(auth_data = Depends(get_user_sede)):
    try:
        res = supabase.table("categorie_costi").select("*").eq("id_sede", auth_data["id_sede"]).execute()
        return res.data
    except Exception as e:
        raise errore_http(e, 'get_categorie', 'Errore nel caricamento delle categorie.', 500)
    
@router.put("/categorie/{id}", status_code=status.HTTP_200_OK)
def update_categoria(id: str, data: CategoriaCostoUpdate, auth_data = Depends(get_user_sede)):
    try:
        update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
        if not update_data:
            return {"message": "Nessun dato da aggiornare."}

        # SICUREZZA: Aggiorna solo se l'ID coincide E se appartiene alla sede dell'utente
        res = supabase.table("categorie_costi").update(update_data)\
            .eq("id", id)\
            .eq("id_sede", auth_data["id_sede"])\
            .execute()
            
        if not res.data:
            raise HTTPException(status_code=404, detail="Categoria non trovata o non autorizzato.")
            
        return {"message": "Categoria aggiornata", "data": res.data[0]}
    except Exception as e:
        raise errore_http(e, 'update_categoria', "Errore durante l'aggiornamento della categoria.", 400)


@router.delete("/categorie/{id}", status_code=status.HTTP_200_OK)
def delete_categoria(id: str, auth_data = Depends(get_user_sede)):
    try:
        # SICUREZZA: Elimina solo se l'ID coincide E se appartiene alla sede dell'utente
        res = supabase.table("categorie_costi").delete()\
            .eq("id", id)\
            .eq("id_sede", auth_data["id_sede"])\
            .execute()
            
        if not res.data:
            raise HTTPException(status_code=404, detail="Categoria non trovata o non autorizzato.")
            
        return {"message": "Categoria eliminata con successo."}
    except Exception as e:
        # Probabile errore se la categoria ha già dei costi associati (Foreign Key constraint)
        raise errore_http(e, 'delete_categoria', 'Impossibile eliminare la categoria.', 400, per_codice={'23503': 'Impossibile eliminare: la categoria è usata da costi registrati.'})


# ---COSTI ANNO MESE---

@router.post("/costi", status_code=status.HTTP_201_CREATED)
def create_costo(data: CostoAnnoMeseCreate, auth_data = Depends(get_user_sede)):
    try:
        insert_data = data.model_dump(mode="json")
        insert_data["id_sede"] = auth_data["id_sede"]

        mese_testuale = data.mese.strip().lower()
        
        numero_mese = MAPPATURA_MESI.get(mese_testuale, "01")
    
        insert_data["anno_mese"] = f"{data.anno}-{numero_mese}"
        # --- FINE MAGIA ---

        res = supabase.table("costi_anno_mese").insert(insert_data).execute()
        return {"message": "Costo registrato", "data": res.data[0]}
    except Exception as e:
        raise errore_http(e, 'create_costo', 'Errore durante il salvataggio del costo.', 400)
    
@router.get("/costi", status_code=status.HTTP_200_OK)
def get_costi(auth_data = Depends(get_user_sede)):
    try:
        res = supabase.table("costi_anno_mese").select("*, categorie_costi(*)").eq("id_sede", auth_data["id_sede"]).execute()
        return res.data
    except Exception as e:
        raise errore_http(e, 'get_costi', 'Errore nel caricamento dei costi.', 500)
          
@router.put("/costi/{id}", status_code=status.HTTP_200_OK)
def update_costo(id: str, data: CostoAnnoMeseUpdate, auth_data = Depends(get_user_sede)):
    try:
        update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
        if not update_data:
            return {"message": "Nessun dato da aggiornare."}

        res = supabase.table("costi_anno_mese").update(update_data)\
            .eq("id", id)\
            .eq("id_sede", auth_data["id_sede"])\
            .execute()
            
        if not res.data:
            raise HTTPException(status_code=404, detail="Costo non trovato o non autorizzato.")
            
        return {"message": "Costo aggiornato", "data": res.data[0]}
    except Exception as e:
        raise errore_http(e, 'update_costo', "Errore durante l'aggiornamento del costo.", 400)

@router.delete("/costi/{id}", status_code=status.HTTP_200_OK)
def delete_costo(id: str, auth_data = Depends(get_user_sede)):
    try:
        res = supabase.table("costi_anno_mese").delete()\
            .eq("id", id)\
            .eq("id_sede", auth_data["id_sede"])\
            .execute()
        return {"message": "Costo eliminato con successo."}
    except Exception as e:
        raise errore_http(e, 'delete_costo', "Errore durante l'eliminazione del costo.", 400)