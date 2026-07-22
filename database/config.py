import os
import httpx
from supabase import create_client, Client, ClientOptions
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Depends

load_dotenv()

class Database:
    _instance: Client = None

    @classmethod
    def get_client(cls) -> Client:
        if cls._instance is None:
            url = os.getenv("SUPABASE_URL")
            key = os.getenv("SUPABASE_KEY")

            if not url or not key:
                raise ValueError("Credenziali Supabase mancanti nel file .env")

            # Client HTTP con connessioni keep-alive riciclate più spesso (10s
            # invece del default httpx più permissivo): dopo operazioni
            # massive (import/cancellazioni da migliaia di righe) si è
            # osservato più volte il client restare agganciato a connessioni
            # ormai chiuse lato Supabase, con risposte imbarcate silenziosamente
            # incomplete (join embedded che tornano null) finché il processo
            # non veniva riavviato. Limitando la vita delle connessioni in pool
            # si riducono le occasioni per ripresentarsi — non è una garanzia
            # assoluta (il sintomo si osserva solo in produzione, non è
            # riproducibile a comando), ma è la mitigazione più mirata e a
            # basso rischio applicabile qui.
            httpx_client = httpx.Client(
                timeout=httpx.Timeout(120),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=10),
            )
            cls._instance = create_client(url, key, options=ClientOptions(httpx_client=httpx_client))

        return cls._instance
