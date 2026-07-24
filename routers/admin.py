from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from utils.auth_utils import get_current_user, get_user_role, ADMIN_ROLE_ID, USER_ROLE_ID
from database.config import Database
from models.chat import ChatStatoUpdate
from routers.chat import _map_chat

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"]
)

supabase = Database.get_client()


def require_admin(current_user=Depends(get_current_user)):
    if get_user_role(current_user.id) != ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="Accesso riservato agli amministratori.")
    return current_user


class BlockStatusUpdate(BaseModel):
    is_blocked: bool


@router.get("/users")
def list_users(_: object = Depends(require_admin)):
    """
    Elenca tutte le utenze "user" (role 2) con la sede assegnata, per la
    sezione admin da cui scegliere quale utente visualizzare e approvare
    (sbloccare) le nuove registrazioni.
    """
    res = supabase.table("users").select(
        "id, nome, cognome, email, telefono, is_blocked, is_verified, id_sede, "
        "sedi(comune, indirizzo, negozi(nome_negozio))"
    ).eq("role", USER_ROLE_ID).execute()

    return res.data or []


@router.put("/users/{user_id}/blocco")
def set_user_block_status(user_id: str, data: BlockStatusUpdate, _: object = Depends(require_admin)):
    """
    Blocca o sblocca un utente. Ogni nuova registrazione nasce bloccata
    (vedi /auth/register); questo è l'unico modo per approvarla, e un admin
    può ribloccare un utente già approvato in qualsiasi momento.
    """
    res = supabase.table("users").update({"is_blocked": data.is_blocked}).eq("id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Utente non trovato.")
    return res.data[0]


@router.get("/chat")
def list_chat(_: object = Depends(require_admin)):
    """
    Elenca TUTTE le conversazioni aperte dagli utenti tramite "Contatta
    l'Amministrazione" nel profilo — non solo le proprie — ordinate per
    stato e ultima attività, con mittente e sede per contesto. La lettura e
    l'invio dei messaggi di ogni conversazione restano su /api/chat/{id}/...,
    condivisi con l'utente proprietario: un admin è già autorizzato lì.
    """
    res = supabase.table("chat_richieste").select(
        "id, ogetto, stato, created_at, updated_at, "
        "users(nome, cognome, email), "
        "sedi(comune, negozi(nome_negozio))"
    ).order("stato").order("updated_at", desc=True).execute()

    return [_map_chat(c) for c in (res.data or [])]


@router.put("/chat/{chat_id}/stato")
def aggiorna_stato_chat(chat_id: str, data: ChatStatoUpdate, _: object = Depends(require_admin)):
    """Un admin apre/chiude una conversazione (aperta <-> risolta)."""
    res = supabase.table("chat_richieste").update({"stato": data.stato}).eq("id", chat_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Conversazione non trovata.")
    return _map_chat(res.data[0])
