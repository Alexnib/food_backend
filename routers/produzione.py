from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.produzione import *
from utils.auth_utils import get_user_sede

router = APIRouter(prefix="/api/produzione", tags=["Produzione e Ricette"])
supabase = Database.get_client()


@router.post("/ricette", status_code=status.HTTP_201_CREATED)
async def create_ricetta(data: RicettaCreate, auth_data = Depends(get_user_sede)):
    try:
        # 1. Creiamo il "contenitore" della ricetta (costo temporaneo 0)
        ricetta_insert = {
            "nome_ricetta": data.nome_ricetta,
            "descrizione_ricetta": data.descrizione_ricetta,
            "id_categoria_prodotto": data.id_categoria_prodotto,
            "id_sede": auth_data["id_sede"],
            "costo_ricetta_reale": 0.0,
            "prezzo_vendita_lordo": data.prezzo_vendita_lordo,
            "prezzo_vendita_netto": data.prezzo_vendita_netto,
            "id_iva_vendita": data.id_iva_vendita
        }
        res_ricetta = supabase.table("ricette").insert(ricetta_insert).execute()
        id_ricetta_creata = res_ricetta.data[0]["id"]

        costo_totale_ricetta = 0.0
        ingredienti_da_inserire = []

        # 2. Calcoliamo il costo di ogni ingrediente e prepariamoli per l'inserimento
        if data.ingredienti:
            for ing in data.ingredienti:
                # Peschiamo il costo unitario della materia prima dal DB
                mp_res = supabase.table("articoli").select("prezzo_acquisto_netto").eq("id", ing.id_materia_prima).execute()
                
                if not mp_res.data:
                    continue # Se non trova la materia prima, la salta
                
                costo_unitario = mp_res.data[0]["prezzo_acquisto_netto"]
                
                # LA MATEMATICA DELLO SCARTO (Es. 20% scarto -> Resa 80% -> 0.8)
                resa = 1 - (ing.perc_scarto / 100)
                quantita_effettiva = (ing.quantita_per_kg / resa) if resa > 0 else ing.quantita_per_kg
                
                # Calcolo del costo di questo singolo ingrediente nella ricetta
                costo_ingrediente = quantita_effettiva * costo_unitario
                costo_totale_ricetta += costo_ingrediente

                # Prepariamo la riga per il database
                ingredienti_da_inserire.append({
                    "id_ricetta": id_ricetta_creata,
                    "id_materia_prima": ing.id_materia_prima,
                    "quantita_per_kg": ing.quantita_per_kg,
                    "perc_scarto": ing.perc_scarto
                })
            
            # Inseriamo tutti gli ingredienti nel DB in un colpo solo (Bulk Insert)
            if ingredienti_da_inserire:
                supabase.table("ingredienti_ricetta").insert(ingredienti_da_inserire).execute()

        # 3. Aggiorniamo la ricetta con il VERO Food Cost e i Margini calcolati
        costo_finale = round(costo_totale_ricetta, 2)

        supabase.table("ricette").update({
            "costo_ricetta_reale": costo_finale,
            "prezzo_vendita_lordo": data.prezzo_vendita_lordo,
            "prezzo_vendita_netto": data.prezzo_vendita_netto,
            "id_iva_vendita": data.id_iva_vendita
        }).eq("id", id_ricetta_creata).execute()

        return {
            "message": "Ricetta creata con successo", 
            "id": id_ricetta_creata,
            "costo_ricetta_reale": costo_finale
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/ricette")
async def get_ricette(auth_data = Depends(get_user_sede)):
    # Restituiamo le ricette e includiamo in automatico i loro ingredienti nidificati e categorie!
    # Paginazione interna per recuperare SEMPRE tutte le righe, anche oltre il cap di righe di
    # PostgREST/Supabase su una singola query (~1000), stesso pattern usato in routers/vendite.py.
    id_sede = auth_data["id_sede"]
    select_query = "*, categoria_prodotti(nome_categoria), ingredienti_ricetta(*, articoli(nome_articolo, unita_misura, prezzo_acquisto_netto))"

    tutte_le_ricette = []
    offset = 0
    page_size = 500  # batch più piccolo: ogni riga include ingredienti nidificati, payload più pesante
    while True:
        batch = supabase.table("ricette").select(select_query).eq("id_sede", id_sede).range(offset, offset + page_size - 1).execute()
        rows = batch.data or []
        tutte_le_ricette.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    return tutte_le_ricette

@router.put("/ricette/{id}")
async def update_ricetta(id: str, data: RicettaCreate, auth_data = Depends(get_user_sede)):
    try:
        # 1. Elimina vecchi ingredienti
        supabase.table("ingredienti_ricetta").delete().eq("id_ricetta", id).execute()
        
        # 2. Ricalcola e reinserisci ingredienti
        costo_totale_ricetta = 0.0
        ingredienti_da_inserire = []

        if data.ingredienti:
            for ing in data.ingredienti:
                mp_res = supabase.table("articoli").select("prezzo_acquisto_netto").eq("id", ing.id_materia_prima).execute()
                if not mp_res.data: continue
                costo_unitario = mp_res.data[0]["prezzo_acquisto_netto"]
                
                resa = 1 - (ing.perc_scarto / 100)
                quantita_effettiva = (ing.quantita_per_kg / resa) if resa > 0 else ing.quantita_per_kg
                costo_ingrediente = quantita_effettiva * costo_unitario
                costo_totale_ricetta += costo_ingrediente

                ingredienti_da_inserire.append({
                    "id_ricetta": id,
                    "id_materia_prima": ing.id_materia_prima,
                    "quantita_per_kg": ing.quantita_per_kg,
                    "perc_scarto": ing.perc_scarto
                })
            
            if ingredienti_da_inserire:
                supabase.table("ingredienti_ricetta").insert(ingredienti_da_inserire).execute()

        costo_finale = round(costo_totale_ricetta, 2)
        
        # 3. Aggiorna dati ricetta
        update_data = {
            "nome_ricetta": data.nome_ricetta,
            "descrizione_ricetta": data.descrizione_ricetta,
            "id_categoria_prodotto": data.id_categoria_prodotto,
            "costo_ricetta_reale": costo_finale,
            "prezzo_vendita_lordo": data.prezzo_vendita_lordo,
            "prezzo_vendita_netto": data.prezzo_vendita_netto,
            "id_iva_vendita": data.id_iva_vendita
        }
        res = supabase.table("ricette").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        
        return {"message": "Ricetta aggiornata", "id": id, "costo_ricetta_reale": costo_finale}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/ricette/{id}")
async def delete_ricetta(id: str, auth_data = Depends(get_user_sede)):
    try:
        supabase.table("ingredienti_ricetta").delete().eq("id_ricetta", id).execute()
        res = supabase.table("ricette").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        return {"message": "Ricetta eliminata"}
    except Exception as e:
        if "23503" in str(e):
            raise HTTPException(status_code=400, detail="Impossibile eliminare: questa ricetta è usata nei prodotti finiti o ha vendite registrate.")
        raise HTTPException(status_code=400, detail=str(e))
