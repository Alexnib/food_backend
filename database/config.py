import os
import time
import httpx
from supabase import create_client, Client, ClientOptions
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Depends

load_dotenv()


class _ClientProxy:
    """
    Sostituto trasparente del Client Supabase concreto.

    Ogni router fa `supabase = Database.get_client()` UNA SOLA VOLTA, quando
    il modulo viene importato all'avvio del server — quel riferimento resta
    fisso per tutta la vita del processo, non viene mai richiesto di nuovo.
    Se get_client() ritornasse direttamente il client concreto, un eventuale
    ricambio periodico del client (vedi Database._real_client) non avrebbe
    ALCUN effetto sui router già importati: userebbero per sempre l'oggetto
    catturato all'avvio, esattamente come prima di questa modifica — è
    l'esatta ragione per cui il tentativo precedente di ricreare il client
    periodicamente non ha avuto alcun effetto osservabile.

    Questo proxy risolve il problema alla radice: `supabase` nei router punta
    sempre a QUESTO stesso oggetto (la sua identità non cambia mai, quindi
    "catturarlo una volta" è innocuo), ma ogni singolo utilizzo — es.
    supabase.table(...) — passa da __getattr__, che chiede a Database il
    client Supabase REALE più aggiornato in quel momento, invece di uno
    congelato all'avvio.
    """
    def __getattr__(self, name):
        return getattr(Database._real_client(), name)


class Database:
    _instance: Client = None
    _created_at: float = 0.0
    _proxy = _ClientProxy()

    # Il client Supabase reale viene ricreato da zero dopo questo tempo,
    # indipendentemente da quanto è stato usato — osservato più volte un
    # degrado nella risoluzione dei join embedded (es. destinazione_prodotto,
    # aliquote IVA) dopo che il processo gira per un po', senza una causa di
    # rete isolabile con certezza dagli strumenti disponibili qui. Ricreare
    # il client è economico (nessuna chiamata di rete finché non viene
    # usato), quindi un intervallo breve non costa nulla in prestazioni.
    _MAX_AGE_SECONDS = 120

    @classmethod
    def _real_client(cls) -> Client:
        now = time.time()
        if cls._instance is None or (now - cls._created_at) > cls._MAX_AGE_SECONDS:
            url = os.getenv("SUPABASE_URL")
            key = os.getenv("SUPABASE_KEY")

            if not url or not key:
                raise ValueError("Credenziali Supabase mancanti nel file .env")

            httpx_client = httpx.Client(timeout=httpx.Timeout(120))
            cls._instance = create_client(url, key, options=ClientOptions(httpx_client=httpx_client))
            cls._created_at = now

        return cls._instance

    @classmethod
    def get_client(cls):
        # Ritorna il proxy stabile, non il client concreto: vedi _ClientProxy.
        return cls._proxy
