# routers/test_router.py
from fastapi import APIRouter, HTTPException
from database.config import Database

# Creiamo un router dedicato ai test
router = APIRouter(prefix="/api/test", tags=["Test Connessione"])

@router.get("/db")
def test_db_connection():
    try:
        # 1. Recuperiamo il client di Supabase
        supabase = Database.get_client()
    
        response = supabase.table("provenienza_prodotto").select("*").limit(1).execute()
        
        # Se arriviamo qui senza errori, la connessione funziona!
        return {
            "status": "success", 
            "message": "Connessione a Supabase riuscita", 
            "dati_trovati": response.data
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Errore di connessione a Supabase: {str(e)}"
        )