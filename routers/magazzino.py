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
# ARTICOLI (Acquisti, Materie Prime e Rivendita)
# ==========================================
@router.post("/articoli", status_code=status.HTTP_201_CREATED)
async def create_articolo(data: ArticoloCreate, auth_data = Depends(get_user_sede)):
    insert_data = data.model_dump(mode="json")
    insert_data["id_sede"] = auth_data["id_sede"]
    
    # Calcolo automatico dei margini usando i prezzi netti, SOLO se è rivendita
    if insert_data.get("is_rivendita"):
        margine, margine_perc = calcola_margini(insert_data["prezzo_vendita_netto"], insert_data["prezzo_acquisto_netto"])
        insert_data["margine"] = margine
        insert_data["margine_perc"] = margine_perc
    else:
        insert_data["margine"] = 0
        insert_data["margine_perc"] = 0

    res = supabase.table("articoli").insert(insert_data).execute()
    return res.data[0]

@router.get("/articoli")
async def get_articoli(auth_data = Depends(get_user_sede)):
    res = supabase.table("articoli").select("*, categoria_prodotti(nome_categoria)").eq("id_sede", auth_data["id_sede"]).execute()
    return res.data

@router.put("/articoli/{id}")
async def update_articolo(id: str, data: ArticoloUpdate, auth_data = Depends(get_user_sede)):
    update_data = {k: v for k, v in data.model_dump(mode="json").items() if v is not None}
    
    # Ricalcola i margini se cambiano
    old_data = supabase.table("articoli").select("prezzo_vendita_netto, prezzo_acquisto_netto, is_rivendita").eq("id", id).execute()
    if old_data.data:
        is_rivendita = update_data.get("is_rivendita", old_data.data[0].get("is_rivendita"))
        if is_rivendita:
            pv = update_data.get("prezzo_vendita_netto", old_data.data[0].get("prezzo_vendita_netto") or 0)
            fc = update_data.get("prezzo_acquisto_netto", old_data.data[0].get("prezzo_acquisto_netto") or 0)
            margine, margine_perc = calcola_margini(pv, fc)
            update_data["margine"] = margine
            update_data["margine_perc"] = margine_perc

    res = supabase.table("articoli").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return res.data[0] if res.data else None

@router.delete("/articoli/{id}")
async def delete_articolo(id: str, auth_data = Depends(get_user_sede)):
    res = supabase.table("articoli").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Articolo eliminato"}