"""
Limite di tentativi per gli endpoint pubblici (login, registrazione, recupero
password), in memoria e senza dipendenze esterne.

Finestra scorrevole per chiave: dopo `max_chiamate` richieste nell'ultima
`finestra_secondi`, le successive ricevono 429 con Retry-After finché la più
vecchia esce dalla finestra.

Limiti di questa versione, accettati:
- lo stato vive nel singolo processo: un riavvio lo azzera, e con più
  istanze del backend ognuna conta per conto suo (per il traffico attuale,
  una sola istanza, va bene; con più istanze servirebbe uno store condiviso
  come Redis);
- l'IP del client si legge da X-Forwarded-For (dietro il proxy di Render):
  se quell'header fosse falsificabile, un attaccante potrebbe aggirare il
  limite PER IP. Per questo gli endpoint sensibili hanno anche un limite
  PER EMAIL, che non dipende dall'IP.
"""
import ipaddress
import threading
import time
from collections import deque
from typing import Optional

from fastapi import HTTPException, Request

_MAX_CHIAVI = 50_000
_PULIZIA_OGNI_SECONDI = 60


class RateLimiter:
    def __init__(self, max_chiamate: int, finestra_secondi: int, messaggio: str):
        self.max_chiamate = max_chiamate
        self.finestra = finestra_secondi
        self.messaggio = messaggio
        self._eventi: dict = {}
        self._lock = threading.Lock()
        self._ultima_pulizia = time.monotonic()

    def _pulisci(self, adesso: float) -> None:
        # Toglie le chiavi ormai fuori finestra, così la memoria resta limitata
        # anche con tantissime chiavi diverse (IP/email casuali).
        if adesso - self._ultima_pulizia < _PULIZIA_OGNI_SECONDI and len(self._eventi) < _MAX_CHIAVI:
            return
        self._ultima_pulizia = adesso
        scadute = [k for k, q in self._eventi.items() if not q or adesso - q[-1] > self.finestra]
        for k in scadute:
            del self._eventi[k]
        if len(self._eventi) >= _MAX_CHIAVI:
            self._eventi.clear()

    def check(self, chiave: str) -> None:
        """Registra una richiesta per `chiave`; solleva 429 se ha superato il limite."""
        adesso = time.monotonic()
        with self._lock:
            self._pulisci(adesso)
            coda = self._eventi.setdefault(chiave, deque())
            while coda and adesso - coda[0] > self.finestra:
                coda.popleft()
            if len(coda) >= self.max_chiamate:
                attesa = int(self.finestra - (adesso - coda[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=self.messaggio,
                    headers={"Retry-After": str(attesa)},
                )
            coda.append(adesso)


def ip_client(request: Request) -> str:
    """IP del client: primo elemento di X-Forwarded-For se è un IP valido (il
    controllo evita che stringhe arbitrarie gonfino la memoria del limitatore),
    altrimenti l'indirizzo della connessione diretta."""
    inoltrato = request.headers.get("x-forwarded-for")
    if inoltrato:
        primo = inoltrato.split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(primo))
        except ValueError:
            pass
    return request.client.host if request.client else "sconosciuto"


def chiave_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


# --- Limiti applicati agli endpoint di routers/auth.py ----------------------
_MSG_LOGIN = "Troppi tentativi di accesso. Riprova tra qualche minuto."
_MSG_REGISTRAZIONE = "Troppe registrazioni da questa connessione. Riprova più tardi."
_MSG_RECUPERO = "Troppe richieste di recupero password. Riprova più tardi."

_login_ip = RateLimiter(10, 60, _MSG_LOGIN)
_login_email = RateLimiter(10, 15 * 60, _MSG_LOGIN)
_registrazione_ip = RateLimiter(5, 60 * 60, _MSG_REGISTRAZIONE)
_recupero_ip = RateLimiter(3, 60 * 60, _MSG_RECUPERO)
_recupero_email = RateLimiter(3, 60 * 60, _MSG_RECUPERO)


def limita_login_ip(request: Request) -> None:
    _login_ip.check(ip_client(request))


def limita_login_email(email: str) -> None:
    _login_email.check(chiave_email(email))


def limita_registrazione_ip(request: Request) -> None:
    _registrazione_ip.check(ip_client(request))


def limita_recupero_ip(request: Request) -> None:
    _recupero_ip.check(ip_client(request))


def limita_recupero_email(email: str) -> None:
    _recupero_email.check(chiave_email(email))
