from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from database.config import Database
from utils.auth_utils import get_user_sede
from utils.ai_parser import parse_excel_with_ai
from typing import List, Optional
import json
import datetime
from models.magazzino import ImportItem, SaveImportRequest

router = APIRouter(prefix="/api/import", tags=["Importazione Massiva"])
supabase = Database.get_client()

@router.post("/upload")
async def upload_excel_for_import(file: UploadFile = File(...), auth_data = Depends(get_user_sede)):
    id_sede = auth_data["id_sede"]
    
    # 1. Ottieni le categorie
    cat_res = supabase.table("categoria_prodotti").select("*").eq("id_sede", id_sede).execute()
    categorie = cat_res.data or []
    
    # 2. Leggi il file
    content = await file.read()
    
    # 3. Manda a Gemini
    try:
        json_str = parse_excel_with_ai(content, file.filename, categorie)
        data = json.loads(json_str)
        return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/save")
async def save_imported_products(request: SaveImportRequest, auth_data = Depends(get_user_sede)):
    id_sede = auth_data["id_sede"]
    
    # Ottieni la tabella IVA per fare il match
    iva_res = supabase.table("iva").select("*").execute()
    iva_list = iva_res.data or []
    
    articoli_to_insert = []
    costi_to_insert = []
    
    mesi_ita = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    now = datetime.datetime.now()
    mese_corrente = mesi_ita[now.month - 1]
    anno_corrente = now.year

    for item in request.prodotti:
        # Trova id_iva
        id_iva = next((i["id"] for i in iva_list if i["iva"] == item.iva_perc), iva_list[0]["id"] if iva_list else None)
        
        if item.tipo == "Materia Prima":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "id_iva_acquisto": id_iva,
                "fornitore": "Sconosciuto",
                "anno": anno_corrente,
                "is_materia_prima": True,
                "is_rivendita": False
            })
        elif item.tipo == "Entrambi":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "prezzo_vendita_netto": 0.0,
                "prezzo_vendita_lordo": 0.0,
                "margine": 0.0,
                "margine_perc": 0.0,
                "id_iva_acquisto": id_iva,
                "id_iva_rivendita": id_iva,
                "id_categoria_prodotto": item.id_categoria,
                "is_materia_prima": True,
                "is_rivendita": True,
                "fornitore": "Sconosciuto",
                "anno": anno_corrente
            })
        elif item.tipo == "Rivendita":
            articoli_to_insert.append({
                "id_sede": id_sede,
                "nome_articolo": item.nome_prodotto,
                "unita_misura": item.unita_misura,
                "prezzo_acquisto_netto": item.costo_netto,
                "prezzo_acquisto_lordo": item.costo_lordo,
                "prezzo_vendita_netto": 0.0,
                "prezzo_vendita_lordo": 0.0,
                "margine": 0.0,
                "margine_perc": 0.0,
                "id_iva_acquisto": id_iva,
                "id_iva_rivendita": id_iva,
                "id_categoria_prodotto": item.id_categoria,
                "is_materia_prima": False,
                "is_rivendita": True
            })
        elif item.tipo == "Costo":
            costi_to_insert.append({
                "id_sede": id_sede,
                "id_categoria": item.id_categoria,
                "note": item.nome_prodotto,
                "importo": item.costo_lordo,
                "mese": mese_corrente,
                "anno": anno_corrente,
                "anno_mese": now.strftime("%Y-%m")
            })
            
    try:
        if articoli_to_insert:
            supabase.table("articoli").insert(articoli_to_insert).execute()
        if costi_to_insert:
            supabase.table("costi_anno_mese").insert(costi_to_insert).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore nel salvataggio: {str(e)}")
        
    return {"message": "Importazione completata con successo", "inseriti_articoli": len(articoli_to_insert), "inseriti_costi": len(costi_to_insert)}
