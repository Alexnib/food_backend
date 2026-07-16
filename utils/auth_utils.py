# utils/auth_utils.py
import time
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database.config import Database

security = HTTPBearer()


class _LocalUser:
    """Oggetto minimale con solo l'id, sufficiente ovunque nel codice si usi current_user.id."""
    def __init__(self, user_id: str):
        self.id = user_id


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Estrae il token e lo valida usando supabase.auth.get_claims(), che verifica il
    JWT localmente (firma + scadenza) usando le JWT Signing Keys del progetto,
    con le chiavi pubbliche (JWKS) cachate in memoria dal client stesso: niente
    chiamata di rete a Supabase Auth su ogni richiesta, solo un refresh periodico
    delle chiavi ogni ~10 minuti.

    Se il progetto avesse ancora token firmati con il vecchio "Legacy JWT Secret"
    (HS256), get_claims() ricade automaticamente sulla verifica remota solo per
    quei token, senza bisogno di alcuna configurazione aggiuntiva da parte nostra.
    """
    token = credentials.credentials
    supabase = Database.get_client()

    try:
        claims_res = supabase.auth.get_claims(token)

        if not claims_res:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessione scaduta o non valida"
            )

        return _LocalUser(claims_res["claims"]["sub"])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token non valido: {str(e)}"
        )


# Cache in-memory dell'id_sede per utente, per evitare una query al DB su ogni
# singola richiesta autenticata (l'id_sede di un utente cambia molto raramente).
_ID_SEDE_CACHE: dict[str, tuple[str, float]] = {}
_ID_SEDE_CACHE_TTL = 300  # 5 minuti


def get_user_sede(current_user = Depends(get_current_user)):
    """
    Recupera l'utente loggato e va a leggere il suo id_sede nel database.
    Se non ha una sede, blocca l'operazione.
    """
    cached = _ID_SEDE_CACHE.get(current_user.id)
    if cached and (time.time() - cached[1]) < _ID_SEDE_CACHE_TTL:
        return {"user_id": current_user.id, "id_sede": cached[0]}

    supabase = Database.get_client()
    user_info = supabase.table("users").select("id_sede").eq("id", current_user.id).execute()

    if not user_info.data or not user_info.data[0].get("id_sede"):
        raise HTTPException(status_code=403, detail="Devi avere una sede assegnata per compiere questa operazione.")

    id_sede = user_info.data[0]["id_sede"]
    _ID_SEDE_CACHE[current_user.id] = (id_sede, time.time())

    return {
        "user_id": current_user.id,
        "id_sede": id_sede
    }
