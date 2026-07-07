import sys
from database.config import Database

def main():
    supabase = Database.get_client()
    try:
        res = supabase.table("ingredienti_ricetta").select("*").limit(1).execute()
        print(res.data)
    except Exception as e:
        print("Errore:", e)

if __name__ == '__main__':
    main()
