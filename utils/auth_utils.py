# utils/auth_utils.py
import time
from typing import Optional
from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database.config import Database

security = HTTPBearer()

ADMIN_ROLE_ID = 1
USER_ROLE_ID = 2


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
# Usata SOLO per il percorso normale (nessun "vedi come"): il percorso admin
# fa sempre una query fresca, per non rischiare di servire dati cachati del
# proprio account al posto di quelli del target, o viceversa.
_ID_SEDE_CACHE: dict[str, tuple[str, float]] = {}
_ID_SEDE_CACHE_TTL = 300  # 5 minuti


def get_user_role(user_id: str) -> Optional[int]:
    supabase = Database.get_client()
    info = supabase.table("users").select("role").eq("id", user_id).execute()
    return info.data[0].get("role") if info.data else None


def get_user_sede(
    request: Request,
    current_user = Depends(get_current_user),
    x_view_as_user_id: Optional[str] = Header(None, alias="X-View-As-User-Id"),
):
    """
    Recupera l'id_sede su cui operare per questa richiesta.

    Caso normale (nessun header "vedi come"): è l'id_sede dell'utente loggato,
    con cache in memoria.

    Caso "vedi come utente X" (header X-View-As-User-Id): riservato agli admin
    (role 1), consentito solo in lettura (GET) e solo verso utenze "user"
    (role 2) — mai verso altri admin, mai per scrivere. Ogni condizione non
    rispettata blocca la richiesta invece di ricadere silenziosamente sui
    dati dell'admin.
    """
    supabase = Database.get_client()

    if x_view_as_user_id:
        if request.method != "GET":
            raise HTTPException(
                status_code=403,
                detail="La modalità 'vedi come' consente solo la visualizzazione, non la modifica dei dati."
            )

        if get_user_role(current_user.id) != ADMIN_ROLE_ID:
            raise HTTPException(status_code=403, detail="Non autorizzato a visualizzare i dati di un altro utente.")

        target_info = supabase.table("users").select("id_sede, role").eq("id", x_view_as_user_id).execute()
        if not target_info.data:
            raise HTTPException(status_code=404, detail="Utente non trovato.")

        target = target_info.data[0]
        if target.get("role") != USER_ROLE_ID:
            raise HTTPException(status_code=403, detail="Puoi visualizzare solo utenze di tipo 'user'.")
        if not target.get("id_sede"):
            raise HTTPException(status_code=403, detail="L'utente selezionato non ha una sede assegnata.")

        return {"user_id": x_view_as_user_id, "id_sede": target["id_sede"]}

    cached = _ID_SEDE_CACHE.get(current_user.id)
    if cached and (time.time() - cached[1]) < _ID_SEDE_CACHE_TTL:
        return {"user_id": current_user.id, "id_sede": cached[0]}

    user_info = supabase.table("users").select("id_sede").eq("id", current_user.id).execute()

    if not user_info.data or not user_info.data[0].get("id_sede"):
        raise HTTPException(status_code=403, detail="Devi avere una sede assegnata per compiere questa operazione.")

    id_sede = user_info.data[0]["id_sede"]
    _ID_SEDE_CACHE[current_user.id] = (id_sede, time.time())

    return {
        "user_id": current_user.id,
        "id_sede": id_sede
    }
