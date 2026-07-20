from fastapi import APIRouter, HTTPException, Depends
from utils.auth_utils import get_current_user, get_user_role, ADMIN_ROLE_ID, USER_ROLE_ID
from database.config import Database

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"]
)

supabase = Database.get_client()


def require_admin(current_user=Depends(get_current_user)):
    if get_user_role(current_user.id) != ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="Accesso riservato agli amministratori.")
    return current_user


@router.get("/users")
def list_users(_: object = Depends(require_admin)):
    """
    Elenca tutte le utenze "user" (role 2) con la sede assegnata, per la
    sezione admin da cui scegliere quale utente visualizzare.
    """
    res = supabase.table("users").select(
        "id, nome, cognome, email, id_sede, sedi(comune, indirizzo, negozi(nome_negozio))"
    ).eq("role", USER_ROLE_ID).execute()

    return res.data or []
