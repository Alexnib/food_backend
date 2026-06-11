import os
from supabase import create_client, Client
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
            
            # Inizializza il client Supabase una volta sola
            cls._instance = create_client(url, key)
            
        return cls._instance
    