from database.config import Database
client = Database.get_client()
res = client.table("iva").select("*").execute()
print(res.data)
