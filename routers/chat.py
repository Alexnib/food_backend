from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from utils.auth_utils import get_current_user, get_user_role, ADMIN_ROLE_ID
from models.chat import ChatCreate, MessaggioCreate

router = APIRouter(prefix="/api/chat", tags=["Chat"])
supabase = Database.get_client()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map_chat(c: dict) -> dict:
    """La colonna in tabella si chiama "ogetto" (refuso storico dello schema
    creato a mano): la traduciamo in "oggetto" qui, così l'API e il frontend
    non devono mai conoscere il nome reale della colonna."""
    c = dict(c)
    c["oggetto"] = c.pop("ogetto", None)
    return c


def _get_chat_autorizzata(chat_id: str, current_user):
    """Ritorna (chat, is_admin_caller) se l'utente corrente è il proprietario
    della conversazione o un admin; altrimenti solleva 404/403. Condivisa tra
    lettura e invio messaggi: stessa regola per entrambi."""
    res = supabase.table("chat_richieste").select("*").eq("id", chat_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Conversazione non trovata.")
    chat = res.data[0]

    is_owner = chat["id_utente"] == current_user.id
    is_admin = get_user_role(current_user.id) == ADMIN_ROLE_ID
    if not is_owner and not is_admin:
        raise HTTPException(status_code=403, detail="Non autorizzato a questa conversazione.")

    return chat, is_admin


@router.post("", status_code=status.HTTP_201_CREATED)
def crea_chat(data: ChatCreate, current_user=Depends(get_current_user)):
    """
    Un utente apre una nuova conversazione con l'amministrazione (sezione
    "Contatta l'Amministrazione" nel profilo), con il primo messaggio già
    incluso. Non richiede una sede assegnata: un utente senza sede deve
    poter comunque scrivere per farsela assegnare.
    """
    user_info = supabase.table("users").select("id_sede").eq("id", current_user.id).execute()
    id_sede = user_info.data[0].get("id_sede") if user_info.data else None

    chat_res = supabase.table("chat_richieste").insert({
        "id_utente": current_user.id,
        "id_sede": id_sede,
        "ogetto": data.oggetto,
    }).execute()
    if not chat_res.data:
        raise HTTPException(status_code=400, detail="Impossibile creare la conversazione.")
    chat = chat_res.data[0]

    msg_res = supabase.table("messaggi_chat").insert({
        "id_chat": chat["id"],
        "response_from": "user",
        "testo": data.messaggio,
    }).execute()

    return {**_map_chat(chat), "messaggi": msg_res.data or []}


@router.get("")
def le_mie_chat(current_user=Depends(get_current_user)):
    """Le conversazioni aperte dall'utente corrente, più recenti prima."""
    res = supabase.table("chat_richieste").select("*") \
        .eq("id_utente", current_user.id).order("updated_at", desc=True).execute()
    return [_map_chat(c) for c in (res.data or [])]


@router.get("/{chat_id}/messaggi")
def get_messaggi(chat_id: str, current_user=Depends(get_current_user)):
    """Tutti i messaggi di una conversazione, in ordine cronologico. Solo il
    proprietario o un admin possono leggerli."""
    _get_chat_autorizzata(chat_id, current_user)
    res = supabase.table("messaggi_chat").select("*") \
        .eq("id_chat", chat_id).order("created_at").execute()
    return res.data or []


@router.post("/{chat_id}/messaggi", status_code=status.HTTP_201_CREATED)
def invia_messaggio(chat_id: str, data: MessaggioCreate, current_user=Depends(get_current_user)):
    """
    Aggiunge un messaggio a una conversazione esistente. I messaggi sono
    immutabili: nessun endpoint di modifica o cancellazione è previsto.
    Un nuovo messaggio del proprietario riapre automaticamente una
    conversazione già segnata come risolta.
    """
    chat, _ = _get_chat_autorizzata(chat_id, current_user)
    is_owner = chat["id_utente"] == current_user.id
    # Un admin che scrive nella propria conversazione (proprietario e admin
    # allo stesso tempo) conta comunque come "user": è la sua richiesta.
    response_from = "user" if is_owner else "admin"

    msg_res = supabase.table("messaggi_chat").insert({
        "id_chat": chat_id,
        "response_from": response_from,
        "testo": data.testo,
    }).execute()
    if not msg_res.data:
        raise HTTPException(status_code=400, detail="Impossibile inviare il messaggio.")

    update_payload = {"updated_at": _now_iso()}
    if response_from == "user" and chat.get("stato") == "risolta":
        update_payload["stato"] = "aperta"
    supabase.table("chat_richieste").update(update_payload).eq("id", chat_id).execute()

    return msg_res.data[0]
