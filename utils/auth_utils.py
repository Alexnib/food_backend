# utils/auth_utils.py
import os
import time
from typing import Optional
from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client
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


def _select_users_or_retry_fresh(select_cols: str, user_id: str):
    """
    SELECT su public.users con lo stesso accorgimento già verificato più
    volte in questa sessione: il client condiviso e a lunga vita di questo
    processo (vedi database/config.py) può occasionalmente tornare 0 righe
    per una query che dovrebbe SEMPRE averne una — un client nuovo di zecca,
    usato una volta e scartato, la stessa identica chiamata la esegue
    sempre correttamente. Qui è particolarmente delicato: questa tabella è
    letta da get_user_role/get_user_sede, cioè su quasi ogni richiesta
    autenticata — un falso vuoto qui blocca un utente vero con un 403/404
    senza alcun motivo reale.
    """
    supabase = Database.get_client()
    res = supabase.table("users").select(select_cols).eq("id", user_id).execute()
    if res.data:
        return res.data
    fresh = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return fresh.table("users").select(select_cols).eq("id", user_id).execute().data


def get_user_role(user_id: str) -> Optional[int]:
    data = _select_users_or_retry_fresh("role", user_id)
    return data[0].get("role") if data else None


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
    if x_view_as_user_id:
        if request.method != "GET":
            raise HTTPException(
                status_code=403,
                detail="La modalità 'vedi come' consente solo la visualizzazione, non la modifica dei dati."
            )

        if get_user_role(current_user.id) != ADMIN_ROLE_ID:
            raise HTTPException(status_code=403, detail="Non autorizzato a visualizzare i dati di un altro utente.")

        target_data = _select_users_or_retry_fresh("id_sede, role", x_view_as_user_id)
        if not target_data:
            raise HTTPException(status_code=404, detail="Utente non trovato.")

        target = target_data[0]
        if target.get("role") != USER_ROLE_ID:
            raise HTTPException(status_code=403, detail="Puoi visualizzare solo utenze di tipo 'user'.")
        if not target.get("id_sede"):
            raise HTTPException(status_code=403, detail="L'utente selezionato non ha una sede assegnata.")

        return {"user_id": x_view_as_user_id, "id_sede": target["id_sede"]}

    cached = _ID_SEDE_CACHE.get(current_user.id)
    if cached and (time.time() - cached[1]) < _ID_SEDE_CACHE_TTL:
        return {"user_id": current_user.id, "id_sede": cached[0]}

    user_data = _select_users_or_retry_fresh("id_sede", current_user.id)

    if not user_data or not user_data[0].get("id_sede"):
        raise HTTPException(status_code=403, detail="Devi avere una sede assegnata per compiere questa operazione.")

    id_sede = user_data[0]["id_sede"]
    _ID_SEDE_CACHE[current_user.id] = (id_sede, time.time())

    return {
        "user_id": current_user.id,
        "id_sede": id_sede
    }
