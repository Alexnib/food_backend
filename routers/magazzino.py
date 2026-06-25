from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.magazzino import *
from utils.auth_utils import get_user_sede

router = APIRouter(prefix="/api/magazzino", tags=["Magazzino"])
supabase = Database.get_client()

def calcola_margini(prezzo_vendita: float, food_cost: float):
    margine = prezzo_vendita - food_cost
    # Evitiamo la divisione per zero se il prezzo è 0
    margine_perc = (margine / prezzo_vendita * 100) if prezzo_vendita > 0 else 0.0
    return round(margine, 2), round(margine_perc, 2)

@router.get("/provenienza")
async def get_provenienza():
    res = supabase.table("provenienza_prodotto").select("*").execute()
    return res.data

@router.get("/iva")
async def get_iva():
    import os
    from supabase import create_client
    local_supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    res = local_supabase.table("iva").select("*").execute()
    return res.data

@router.post("/categorie", status_code=status.HTTP_201_CREATED)
async def create_categoria(data: CategoriaProdottoCreate, auth_data = Depends(get_user_sede)):
    insert_data = data.model_dump(mode="json")
    insert_data["id_sede"] = auth_data["id_sede"]
    
    if insert_data.get("id_macro_categoria") is None:
        insert_data["id_macro_categoria"] = 1
        
    res = supabase.table("categoria_prodotti").insert(insert_data).execute()
    return res.data[0]

@router.get("/categorie")
async def get_categorie(auth_data = Depends(get_user_sede)):
    res = supabase.table("categoria_prodotti").select("*, provenienza_prodotto(*)").eq("id_sede", auth_data["id_sede"]).execute()
    return res.data

@router.get("/categorie/rivendita")
async def get_categorie_rivendita(auth_data = Depends(get_user_sede)):
    res = supabase.table("categoria_prodotti").select("*, provenienza_prodotto(*)").eq("id_sede", auth_data["id_sede"]).eq("id_macro_categoria", 1).execute()
    return res.data

@router.get("/categorie/ricette")
async def get_categorie_ricette(auth_data = Depends(get_user_sede)):
    res = supabase.table("categoria_prodotti").select("*, provenienza_prodotto(*)").eq("id_sede", auth_data["id_sede"]).eq("id_macro_categoria", 2).execute()
    return res.data

@router.put("/categorie/{id}")
async def update_categoria(id: int, data: CategoriaProdottoUpdate, auth_data = Depends(get_user_sede)):
    update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
    res = supabase.table("categoria_prodotti").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return res.data[0] if res.data else None

@router.delete("/categorie/{id}")
async def delete_categoria(id: int, auth_data = Depends(get_user_sede)):
    res = supabase.table("categoria_prodotti").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Categoria eliminata"}

# ==========================================
# MATERIE PRIME
# ==========================================
@router.post("/materie-prime", status_code=status.HTTP_201_CREATED)
async def create_materia_prima(data: MateriaPrimaCreate, auth_data = Depends(get_user_sede)):
    insert_data = data.model_dump(mode="json")
    insert_data["id_sede"] = auth_data["id_sede"]
    res = supabase.table("anagrafica_materia_prima").insert(insert_data).execute()
    return res.data[0]

@router.get("/materie-prime")
async def get_materie_prime(auth_data = Depends(get_user_sede)):
    res = supabase.table("anagrafica_materia_prima").select("*").eq("id_sede", auth_data["id_sede"]).execute()
    return res.data

@router.put("/materie-prime/{id}")
async def update_materia_prima(id: str, data: MateriaPrimaUpdate, auth_data = Depends(get_user_sede)):
    update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
    res = supabase.table("anagrafica_materia_prima").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return res.data[0] if res.data else None

@router.delete("/materie-prime/{id}")
async def delete_materia_prima(id: str, auth_data = Depends(get_user_sede)):
    res = supabase.table("anagrafica_materia_prima").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Materia prima eliminata"}

# ==========================================
# ANAGRAFICA RIVENDITA (Prodotti Commerciali)
# ==========================================
@router.post("/rivendita", status_code=status.HTTP_201_CREATED)
async def create_rivendita(data: ProdottoRivenditaCreate, auth_data = Depends(get_user_sede)):
    insert_data = data.model_dump(mode="json")
    insert_data["id_sede"] = auth_data["id_sede"]
    
    # Calcolo automatico dei margini usando i prezzi netti
    margine, margine_perc = calcola_margini(insert_data["prezzo_vendita_netto"], insert_data["prezzo_acquisto_netto"])
    insert_data["margine"] = margine
    insert_data["margine_perc"] = margine_perc

    res = supabase.table("anagrafica_rivendita").insert(insert_data).execute()
    return res.data[0]

@router.get("/rivendita")
async def get_rivendita(auth_data = Depends(get_user_sede)):
    res = supabase.table("anagrafica_rivendita").select("*, categoria_prodotti(nome_categoria)").eq("id_sede", auth_data["id_sede"]).execute()
    return res.data

@router.put("/rivendita/{id}")
async def update_rivendita(id: str, data: ProdottoRivenditaUpdate, auth_data = Depends(get_user_sede)):
    update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
    
    # Ricalcola i margini se uno dei due prezzi netti viene modificato
    if "prezzo_vendita_netto" in update_data or "prezzo_acquisto_netto" in update_data:
        # Recupera il prodotto attuale per i valori non modificati
        old_data = supabase.table("anagrafica_rivendita").select("prezzo_vendita_netto, prezzo_acquisto_netto").eq("id", id).execute()
        if old_data.data:
            pv = update_data.get("prezzo_vendita_netto", old_data.data[0]["prezzo_vendita_netto"])
            fc = update_data.get("prezzo_acquisto_netto", old_data.data[0]["prezzo_acquisto_netto"])
            margine, margine_perc = calcola_margini(pv, fc)
            update_data["margine"] = margine
            update_data["margine_perc"] = margine_perc

    res = supabase.table("anagrafica_rivendita").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return res.data[0] if res.data else None

@router.delete("/rivendita/{id}")
async def delete_rivendita(id: str, auth_data = Depends(get_user_sede)):
    res = supabase.table("anagrafica_rivendita").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Prodotto eliminato"}