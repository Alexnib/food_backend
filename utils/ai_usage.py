"""
Guardia di quota leggera per le chiamate AI (scanner scontrini, import Excel
vendite/materie prime, import fatture). Vedi sql/015_ai_usage_log.sql.

Nessuno dei tre flussi aveva un limite su quante volte possono essere
chiamati: un caricamento ripetuto (per errore o di proposito) genera costo
Gemini illimitato. La soglia (50 chiamate/ora per sede, su tutti i flussi
insieme) è deliberatamente generosa: serve solo a fermare un uso fuori scala,
non a intralciare l'uso normale.
"""
import logging
import time

from database.config import Database
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_LIMITE_ORARIO = 50

# Se la tabella non esiste ancora (script sql/015 non eseguito), niente
# controllo di quota: stesso principio di degrado sicuro già usato per la
# RPC di salvataggio import materie prime (sql/014) — a differenza della
# race condition di sql/013, qui non c'è un bug attivo da bloccare finché
# lo script non viene eseguito. Ricontrolliamo periodicamente invece di
# considerarla mancante per sempre, così quando l'utente esegue lo script la
# guardia entra in funzione da sola.
_tabella_assente_dal = None
_RECHECK_TTL = 120


def check_and_log_ai_usage(id_sede: str, endpoint: str) -> None:
    """Solleva HTTPException(429) se la sede ha superato la soglia oraria;
    altrimenti registra questa chiamata e ritorna normalmente. Un problema
    nel controllo stesso (tabella assente, errore di rete) non deve MAI
    bloccare una scansione/import altrimenti valida: si logga e si prosegue.
    """
    global _tabella_assente_dal

    if _tabella_assente_dal is not None and (time.time() - _tabella_assente_dal) < _RECHECK_TTL:
        return

    supabase = Database.get_client()
    try:
        un_ora_fa = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600))
        res = (
            supabase.table("ai_usage_log")
            .select("id", count="exact")
            .eq("id_sede", id_sede)
            .gte("created_at", un_ora_fa)
            .execute()
        )
        conteggio = res.count if res.count is not None else 0
        _tabella_assente_dal = None
    except Exception as e:
        # Tipicamente la tabella non esiste ancora: degradiamo in sicurezza,
        # senza bloccare l'operazione AI richiesta.
        logger.warning("check_and_log_ai_usage: guardia di quota non disponibile (%s)", e)
        _tabella_assente_dal = time.time()
        return

    if conteggio >= _LIMITE_ORARIO:
        raise HTTPException(
            status_code=429,
            detail=f"Troppe richieste AI nell'ultima ora per questa sede (limite {_LIMITE_ORARIO}). Riprova più tardi.",
        )

    try:
        supabase.table("ai_usage_log").insert({"id_sede": id_sede, "endpoint": endpoint}).execute()
    except Exception as e:
        # Il logging della chiamata è un "best effort": se fallisce non deve
        # impedire la scansione/import già autorizzati dal controllo sopra.
        logger.warning("check_and_log_ai_usage: impossibile registrare la chiamata (%s)", e)
