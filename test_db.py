import os
from database.config import Database
from dotenv import load_dotenv

load_dotenv()
supabase = Database.get_client()

try:
    res = supabase.table("vendite").select("*, ricette(nome_ricetta), articoli(nome_articolo)").limit(1).execute()
    print("Success:")
    print(res.data)
except Exception as e:
    print("Error:")
    print(e)
