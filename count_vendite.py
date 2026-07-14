import os
from supabase import create_client

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

res = supabase.table("vendite").select("id", count="exact").execute()
print(f"Total rows: {res.count}")
