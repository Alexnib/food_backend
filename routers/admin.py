from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from utils.auth_utils import get_current_user, get_user_role, ADMIN_ROLE_ID, USER_ROLE_ID
from utils.db_fetch import call_rpc_or_none
from database.config import Database
from models.chat import ChatStatoUpdate
from routers.chat import _map_chat

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"]
)

supabase = Database.get_client()


def require_admin(current_user=Depends(get_current_user)):
    ruolo = get_user_role(current_user.id)
    # Log temporaneo per diagnosticare la lista utenti/chat vuota subito dopo
    # il login (segnalato dall'utente) — da togliere quando non serve più.
    print(f"[ADMIN] require_admin: user_id={current_user.id} ruolo_risolto={ruolo!r} (ADMIN_ROLE_ID={ADMIN_ROLE_ID!r})")
    if ruolo != ADMIN_ROLE_ID:
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

    Percorso veloce: get_admin_users_list (sql/011) fa il join con sedi/negozi
    dentro Postgres, evitando il resource embedding di PostgREST (lo stesso
    tipo già mostratosi inaffidabile altrove, vedi database/config.py — ed è
    il sintomo osservato qui: lista utenti a volte vuota o incompleta finché
    non passano un paio di minuti). Fallback identico se non ancora creata.
    """
    rows = call_rpc_or_none("get_admin_users_list", {"p_role_id": USER_ROLE_ID}, order_cols=["id"], retry_if_empty=True)
    if rows is not None:
        print(f"[ADMIN] list_users: percorso RPC, righe={len(rows)}")
        return rows

    print("[ADMIN] list_users: RPC non disponibile, fallback a select embedded")
    res = supabase.table("users").select(
        "id, nome, cognome, email, telefono, is_blocked, is_verified, id_sede, "
        "sedi(comune, indirizzo, negozi(nome_negozio))"
    ).eq("role", USER_ROLE_ID).execute()

    print(f"[ADMIN] list_users: fallback, righe={len(res.data or [])}")
    return res.data or []


@router.get("/users/{user_id}")
def get_user_detail(user_id: str, _: object = Depends(require_admin)):
    """
    Profilo completo di un singolo utente (role 2), per il pulsante
    "Informazioni" nella sezione Admin — a differenza di list_users (che
    resta volutamente leggera per la tabella), qui servono anche i campi non
    mostrati in lista: data di registrazione, stato di verifica, indirizzo
    completo della sede, dati del negozio (partita IVA compresa).
    """
    res = supabase.table("users").select(
        "id, nome, cognome, email, telefono, is_blocked, is_verified, auth_provider, created_at, id_sede, "
        "sedi(comune, indirizzo, civico, nome_responsabile, cognome_responsabile, created_at, "
        "negozi(nome_negozio, partita_iva, created_at))"
    ).eq("id", user_id).eq("role", USER_ROLE_ID).execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Utente non trovato.")
    return res.data[0]


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
