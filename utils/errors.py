"""
Conversione delle eccezioni in errori HTTP SENZA esporre al client il testo
interno (messaggi di Python, di PostgREST/Postgres o di librerie): quel testo
può rivelare nomi di tabelle, colonne, vincoli o dettagli dell'infrastruttura.
Il dettaglio completo va solo nei log del server; all'utente arriva un
messaggio scritto da noi, in italiano.
"""
import logging
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger("gest.errori")

# Codici di errore Postgres che l'utente può capire e correggere: per questi
# vale un messaggio in italiano al posto di quello generico.
_MESSAGGI_POSTGRES = {
    "23503": "L'operazione non è possibile perché l'elemento è collegato ad altri dati.",
    "23505": "Esiste già un elemento con gli stessi dati.",
    "23502": "Manca un dato obbligatorio.",
    "23514": "Uno dei valori inseriti non è valido.",
    "22P02": "Uno dei valori inseriti ha un formato non valido.",
    "22007": "Una data inserita ha un formato non valido.",
    "22008": "Una data inserita non è valida.",
}


def errore_http(
    e: Exception,
    contesto: str,
    messaggio: str,
    status_code: int = 400,
    per_codice: Optional[dict] = None,
) -> HTTPException:
    """Da usare come `raise errore_http(e, "nome operazione", "Messaggio generico.")`.

    - Un HTTPException già costruito (es. un 404 sollevato dentro il try) passa
      invariato: prima molti `except Exception` lo riscrivevano in un 400 col
      testo grezzo "404: ...".
    - Un errore Postgres noto usa il suo messaggio in italiano; `per_codice`
      permette di sovrascriverlo per una specifica operazione (es. "impossibile
      eliminare" per un vincolo di chiave esterna).
    - Qualunque altro errore usa `messaggio`. In ogni caso il dettaglio
      completo viene registrato nei log.
    """
    if isinstance(e, HTTPException):
        return e
    logger.error("%s: %s", contesto, e, exc_info=e)
    codice = getattr(e, "code", None)
    testo = (per_codice or {}).get(codice) or _MESSAGGI_POSTGRES.get(codice) or messaggio
    return HTTPException(status_code=status_code, detail=testo)


def messaggio_pubblico(e: Exception, contesto: str, messaggio: str) -> str:
    """Come errore_http, ma per gli stream NDJSON degli import (dove l'errore
    viaggia come riga {"error": ...} e non come HTTPException).

    Un ValueError è, per convenzione in questo progetto, un errore di
    validazione con un messaggio scritto da noi (es. "troppe righe"): resta
    com'è. Tutto il resto è imprevisto: messaggio generico + log.
    """
    if isinstance(e, ValueError):
        return str(e)
    logger.error("%s: %s", contesto, e, exc_info=e)
    return messaggio
