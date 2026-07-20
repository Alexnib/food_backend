from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from utils.auth_utils import get_current_user, security
from models.auth import UserRegister, UserLogin, RefreshTokenRequest, ForgotPassword, UpdatePassword, UpdateUser
from fastapi.responses import RedirectResponse
from supabase import create_client
from database.config import Database
import os
import logging

router = APIRouter(
    prefix="/auth",
    tags=["Autenticazione"]
)

supabase = Database.get_client()

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserRegister):
    try:
        check_email = supabase.table("users").select("id").eq("email", user.email).execute()
        
        if check_email.data:
            # Se la lista contiene almeno un elemento, l'email esiste già
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Un account con questa email è già registrato."
            )
        
        auth_res = supabase.auth.sign_up({
            "email": user.email,
            "password": user.password,
            "options": {
                "data": {
                    "nome": user.nome,
                    "cognome": user.cognome,
                    "role": 2,  # sempre "user" (id 2 in roles): mai scelto dal chiamante
                    "telefono": user.cellulare,
                    "id_sede": str(user.id_sede) if user.id_sede else None
                }
            }
        })

        if not auth_res.user:
            raise HTTPException(status_code=400, detail="Errore durante la registrazione.")

        return {
            "message": "Utente registrato con successo", 
            "user_id": auth_res.user.id
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.post("/login")
def login(credentials: UserLogin):
    # BLOCCO 1: Autenticazione (Supabase Auth)
    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": credentials.email,
            "password": credentials.password
        })
        logging.info(f"Login attempt for {credentials.email}: {'Success' if auth_res.user else 'Failed'}")
    except Exception as e:
        logging.info(f"Tentativo di login fallito per {credentials.email}: {e}") 
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Email o password non valide."
        )
    try:
        user_info = supabase.table("users").select("*").eq("id", auth_res.user.id).execute()
        user_data = user_info.data[0] if user_info.data else None

        if not user_data:
            raise HTTPException(status_code=404, detail="Profilo utente non trovato nel database")
        
        logging.info(f"Recupero profilo per {credentials.email}: {'Success' if user_info.data else 'No data'}")

        if user_data.get("id_sede"):
            sede_info = supabase.table("sedi").select("*, negozi(*)").eq("id", user_data["id_sede"]).execute()
            user_data["sedi"] = sede_info.data[0] if sede_info.data else None
        else:
            user_data["sedi"] = None 

        return {
            "message": "Login effettuato",
            "access_token": auth_res.session.access_token,
            "refresh_token": auth_res.session.refresh_token,
            "user": user_data
        }
    except Exception as e:
        print(f"Errore DB durante il recupero del profilo di {auth_res.user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Errore interno nel recupero del profilo utente"
        )
        
@router.post("/refresh-token")
def refresh_token(data: RefreshTokenRequest):
    """
    Scambia un refresh_token valido con un nuovo access_token fresco.
    """
    try:
        # Chiediamo a Supabase di rinnovare la sessione usando il refresh token
        res = supabase.auth.refresh_session(data.refresh_token)
        
        if not res.session:
             raise HTTPException(status_code=401, detail="Impossibile rinnovare la sessione.")

        return {
            "message": "Token rinnovato con successo",
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token, # Supabase te ne dà anche uno nuovo!
            "user": res.user
        }
    except Exception as e:
        # Se il refresh token è scaduto, contraffatto o revocato, costringiamo al re-login
        print(f"Errore durante il refresh del token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Sessione scaduta in modo permanente. Effettua nuovamente il login."
        )
        
@router.post("/logout")
def logout(current_user = Depends(get_current_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Revoca la sessione lato Supabase (tutti i refresh token dell'utente, scope
    "global"): senza questo, il logout era puramente client-side e un refresh
    token rimasto salvato altrove avrebbe continuato a funzionare.
    """
    try:
        admin_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        admin_client.auth.admin.sign_out(credentials.credentials, "global")
    except Exception as e:
        # Il logout locale deve comunque riuscire: la revoca è un "best effort".
        logging.warning(f"Impossibile revocare la sessione Supabase per {current_user.id}: {e}")

    return {"message": "Logout effettuato"}

@router.get("/google/login")
def google_login():
    """
    Endpoint per avviare il flusso OAuth2 di Google.
    Il frontend chiamerà questo URL per farsi reindirizzare alla pagina di consenso di Google.
    """
    try:
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
        res = supabase.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {
                # Dove Supabase rimanderà l'utente dopo il login su Google
                "redirect_to": f"{frontend_url}/auth/callback"
            }
        })
        return {"url": res.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/forgot-password")
def forgot_password(data: ForgotPassword):
    """
    1. Richiesta di Reset Password (Pubblica).
    Invia un'email con un link univoco all'utente.
    """
    try:
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        
        # Supabase invierà una mail con il link che reindirizza al tuo frontend
        supabase.auth.reset_password_email(
            data.email,
            options={"redirect_to": f"{frontend_url}/auth/reset-password"}
        )
        # SICUREZZA: Rispondiamo sempre con lo stesso messaggio per evitare
        # la "Account Enumeration" (impedire agli hacker di scoprire quali email esistono).
        return {"message": "Se l'email è registrata, riceverai a breve un link per reimpostare la password."}
    except Exception as e:
        return {"message": "Se l'email è registrata, riceverai a breve un link per reimpostare la password."}

@router.put("/me/password")
def update_password(data: UpdatePassword, current_user = Depends(get_current_user)):
    """
    2. Cambio Password (Protetta).
    Cambia la password dell'utente attualmente loggato.
    """
    try:
        # Usiamo l'API Admin di Supabase per sovrascrivere la password 
        # dell'utente di cui abbiamo validato l'identità tramite il JWT.
        admin_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        
        admin_client.auth.admin.update_user_by_id(
            current_user.id,
            {"password": data.new_password}
        )
        return {"message": "Password aggiornata con successo."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore durante il cambio password: {str(e)}")

@router.put("/me")
def update_profile(data: UpdateUser, current_user = Depends(get_current_user)):
    """
    3. Modifica Dati Utente (Protetta).
    Aggiorna nome, cognome o telefono nella tabella pubblica.
    """
    try:
        update_data = {k: v for k, v in data.model_dump().items() if v is not None}

        if not update_data:
            return {"message": "Nessun dato fornito per l'aggiornamento."}

        admin_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

        # Eseguiamo gli aggiornamenti usando l'admin_client invece del client globale
        res = admin_client.table("users").update(update_data).eq("id", current_user.id).execute()

        admin_client.auth.admin.update_user_by_id(
            current_user.id,
            {"user_metadata": update_data}
        )

        return {
            "message": "Profilo aggiornato con successo.", 
            "user": res.data[0] if res.data else None
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore aggiornamento profilo: {str(e)}")

@router.delete("/me")
def delete_account(current_user = Depends(get_current_user)):
    try:
        # 🪄 CLIENT ISOLATO: Creiamo un client usa-e-getta con pieni poteri
        admin_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

        # Eseguiamo le eliminazioni usando l'admin_client invece del client globale
        admin_client.table("users").delete().eq("id", current_user.id).execute()
        admin_client.auth.admin.delete_user(current_user.id)

        return {"message": "Account eliminato definitivamente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore durante l'eliminazione dell'account: {str(e)}")