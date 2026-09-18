from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from utils.auth_utils import get_current_user, security
from utils.errors import errore_http
from utils.rate_limit import (
    limita_login_ip, limita_login_email, limita_registrazione_ip,
    limita_recupero_ip, limita_recupero_email,
)
from models.auth import UserRegister, UserLogin, RefreshTokenRequest, ForgotPassword, UpdatePassword, UpdateUser
from fastapi.responses import RedirectResponse
from supabase import create_client
from supabase_auth.errors import AuthApiError
from database.config import Database
import os
import sys
import time
import logging

# Logger dedicato agli accessi, visibile nei log di Render. Il resto del
# codice usa logging.info(), che con la configurazione di default di Python
# (solo warning e superiori) non compare nei log: un logger con un handler
# proprio su stdout rende visibili solo queste righe, senza cambiare il
# livello dei log dell'intera app. Registra SOLO l'email e l'esito: mai
# password, token o altri dati.
login_logger = logging.getLogger("gest.login")
if not login_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("[LOGIN] %(message)s"))
    login_logger.addHandler(_handler)
login_logger.setLevel(logging.INFO)
login_logger.propagate = False

router = APIRouter(
    prefix="/auth",
    tags=["Autenticazione"]
)

supabase = Database.get_client()

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserRegister, _: None = Depends(limita_registrazione_ip)):
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

        # Ogni nuovo utente nasce bloccato: resta in attesa che un admin lo
        # sblocchi dalla sezione Admin, anche dopo aver confermato l'email.
        # La riga in public.users viene creata da un trigger sull'insert in
        # auth.users, nella stessa transazione di sign_up(): dovrebbe già
        # esistere, ma un breve retry rende l'operazione robusta a eventuali
        # ritardi di propagazione senza far fallire la registrazione stessa.
        block_res = supabase.table("users").update({"is_blocked": True}).eq("id", auth_res.user.id).execute()
        if not block_res.data:
            time.sleep(0.5)
            block_res = supabase.table("users").update({"is_blocked": True}).eq("id", auth_res.user.id).execute()
        if not block_res.data:
            logging.error(f"Impossibile impostare is_blocked per il nuovo utente {auth_res.user.id}")

        return {
            "message": "Utente registrato con successo",
            "user_id": auth_res.user.id
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        raise errore_http(e, 'register', 'Errore durante la registrazione.', 400, per_codice={'user_already_exists': 'Un account con questa email è già registrato.', 'email_exists': 'Un account con questa email è già registrato.', 'weak_password': 'La password scelta è troppo debole: provane una più complessa.', 'email_address_invalid': 'Indirizzo email non valido.', 'signup_disabled': 'Le registrazioni sono al momento disabilitate.', 'over_email_send_rate_limit': 'Troppe richieste di registrazione. Riprova più tardi.', 'over_request_rate_limit': 'Troppe richieste. Riprova più tardi.'})
    
@router.post("/login")
def login(credentials: UserLogin, _: None = Depends(limita_login_ip)):
    # Limite per email oltre a quello per IP: protegge il singolo account da
    # tentativi ripetuti anche se arrivano da IP diversi.
    limita_login_email(credentials.email)

    # BLOCCO 1: Autenticazione (Supabase Auth)
    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": credentials.email,
            "password": credentials.password
        })
    except AuthApiError as e:
        login_logger.info("Accesso fallito: %s", credentials.email)
        # Prima qualunque errore di sign_in_with_password (password sbagliata,
        # email non confermata, ecc.) diventava lo stesso generico "email o
        # password non valide" — chi si registra e non conferma l'email
        # riceveva un messaggio che sembrava dire "hai sbagliato password",
        # non "devi prima confermare l'email".
        if e.code == "email_not_confirmed":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Devi prima confermare la tua email: controlla la tua casella di posta (anche lo spam) e clicca sul link di conferma."
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o password non valide."
        )
    except Exception as e:
        login_logger.info("Accesso fallito: %s", credentials.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o password non valide."
        )
    try:
        user_info = supabase.table("users").select("*").eq("id", auth_res.user.id).execute()
        user_data = user_info.data[0] if user_info.data else None

        if not user_data:
            raise HTTPException(status_code=404, detail="Profilo utente non trovato nel database")

        if user_data.get("is_blocked"):
            # L'autenticazione è riuscita, ma l'utente non è ancora stato
            # approvato da un admin: revochiamo comunque la sessione appena
            # creata, così il token emesso non resta valido inutilizzato.
            try:
                admin_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
                admin_client.auth.admin.sign_out(auth_res.session.access_token, "global")
            except Exception as e:
                logging.warning(f"Impossibile revocare la sessione dell'utente bloccato {auth_res.user.id}: {e}")
            login_logger.info("Accesso negato (in attesa di approvazione): %s", credentials.email)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Il tuo account è in attesa di approvazione da parte di un amministratore."
            )

        logging.info(f"Recupero profilo per {credentials.email}: {'Success' if user_info.data else 'No data'}")

        if user_data.get("id_sede"):
            sede_info = supabase.table("sedi").select("*, negozi(*)").eq("id", user_data["id_sede"]).execute()
            user_data["sedi"] = sede_info.data[0] if sede_info.data else None
        else:
            user_data["sedi"] = None

        login_logger.info("Accesso effettuato: %s", credentials.email)
        return {
            "message": "Login effettuato",
            "access_token": auth_res.session.access_token,
            "refresh_token": auth_res.session.refresh_token,
            "user": user_data
        }
    except HTTPException:
        raise
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
        raise errore_http(e, 'google_login', "Impossibile avviare l'accesso con Google.", 500)
    
@router.post("/forgot-password")
def forgot_password(data: ForgotPassword, _: None = Depends(limita_recupero_ip)):
    """
    1. Richiesta di Reset Password (Pubblica).
    Invia un'email con un link univoco all'utente.
    """
    # Limite per email (oltre a quello per IP), applicato a QUALUNQUE indirizzo
    # esista o no: non rivela quali email sono registrate, e impedisce di
    # inondare di email di reset la casella di una persona. Fuori dal try: il
    # 429 non deve essere inghiottito dal "risposta sempre uguale" qui sotto.
    limita_recupero_email(data.email)

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
def update_password(
    data: UpdatePassword,
    current_user = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    2. Cambio Password (Protetta).
    Cambia la password dell'utente attualmente loggato (sia dal profilo che
    dal link "reimposta password" ricevuto per email, che arriva qui con un
    token di recupero temporaneo invece della sessione normale).
    """
    try:
        # Prima si usava l'API Admin (admin.update_user_by_id): funziona per
        # cambiare la password, ma è un percorso diverso da quello
        # "self-service" (PUT /auth/v1/user) a cui è legata la notifica email
        # nativa di Supabase "Password changed" — con l'Admin API la password
        # cambiava ma l'email di notifica non partiva mai. Impersoniamo quindi
        # l'utente con il suo stesso token (già validato da get_current_user)
        # e chiamiamo l'update self-service, che fa scattare la notifica.
        user_client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        user_client.auth.set_session(credentials.credentials, credentials.credentials)
        user_client.auth.update_user({"password": data.new_password})
        return {"message": "Password aggiornata con successo."}
    except AuthApiError as e:
        # Messaggi di Supabase arrivano solo in inglese: traduciamo i codici
        # che un utente può realmente incontrare qui, il resto resta un
        # messaggio generico piuttosto che l'inglese grezzo di Supabase.
        messaggi = {
            "same_password": "La nuova password deve essere diversa da quella attuale.",
            "weak_password": "La password scelta è troppo debole: provane una più complessa.",
        }
        raise HTTPException(
            status_code=400,
            detail=messaggi.get(e.code, "Errore durante il cambio password.")
        )
    except Exception as e:
        raise errore_http(e, 'update_password', 'Errore durante il cambio password.', 400)

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
        raise errore_http(e, 'update_profile', "Errore durante l'aggiornamento del profilo.", 400)


# Nessuna auto-cancellazione: un utente non può eliminare il proprio account.
# Solo il blocco/sblocco da parte di un admin (vedi /api/admin/users/{id}/blocco).