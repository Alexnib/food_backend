# utils/auth_utils.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database.config import Database

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Estrae il token, lo invia a Supabase per la verifica e restituisce l'utente.
    Se il token è falso o scaduto, blocca la richiesta con un errore 401.
    """
    token = credentials.credentials
    supabase = Database.get_client()

    try:
        # Chiediamo a Supabase di decifrare e validare il token
        user_res = supabase.auth.get_user(token)
        
        if not user_res.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail="Sessione scaduta o non valida"
            )
            
        return user_res.user # Restituisce l'oggetto user (con l'id, email, ecc.)
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail=f"Token non valido: {str(e)}"
        )
        
def get_user_sede(current_user = Depends(get_current_user)):
    """
    Recupera l'utente loggato e va a leggere il suo id_sede nel database.
    Se non ha una sede, blocca l'operazione.
    """
    supabase = Database.get_client()
    user_info = supabase.table("users").select("id_sede").eq("id", current_user.id).execute()
    
    if not user_info.data or not user_info.data[0].get("id_sede"):
        raise HTTPException(status_code=403, detail="Devi avere una sede assegnata per compiere questa operazione.")
        
    return {
        "user_id": current_user.id,
        "id_sede": user_info.data[0]["id_sede"]
    }