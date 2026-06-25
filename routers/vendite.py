from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.vendite import *
from utils.auth_utils import get_user_sede
from fastapi.responses import StreamingResponse
import io
import pandas as pd
from datetime import date
from openpyxl.styles import Font

router = APIRouter(prefix="/api/vendite", tags=["Vendite"])
supabase = Database.get_client()

@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def registra_vendite_bulk(data: VenditaBulkPayload, auth_data=Depends(get_user_sede)):
    """
    Salvataggio massivo proveniente dall'AI Scanner.
    Ogni item nell'array viene trasformato nel formato VenditaCreate e inserito nel DB.
    """
    try:
        if not data.items:
            raise HTTPException(status_code=400, detail="Nessun item da salvare.")

        # Step 1: Aggrega gli item in ingresso per data e prodotto
        aggregated_items = {}
        for item in data.items:
            # Create a unique key for each group
            key = f"{item.data_vendita.isoformat()}_{item.id_tipo}_{item.id_prodotto_menu}"
            if key in aggregated_items:
                aggregated_items[key].quantita += item.quantita
            else:
                import copy
                aggregated_items[key] = copy.deepcopy(item)

        # Step 2: Elabora ciascun item aggregato contro il database
        results = []
        for key, item in aggregated_items.items():
            data_vendita_iso = item.data_vendita.isoformat()
            id_ricetta = item.id_prodotto_menu if item.id_tipo == "finito" else None
            id_commerciale = item.id_prodotto_menu if item.id_tipo == "commerciale" else None
            
            # Cerca record esistente
            query = supabase.table("vendite").select("*").eq("id_sede", auth_data["id_sede"]).eq("data_vendita", data_vendita_iso)
            if id_ricetta:
                query = query.eq("id_ricetta", id_ricetta)
            elif id_commerciale:
                query = query.eq("id_prodotto_commerciale", id_commerciale)
                
            existing = query.execute()
            
            if existing.data and len(existing.data) > 0:
                # Update
                existing_record = existing.data[0]
                new_quantita = existing_record["quantita"] + item.quantita
                res = supabase.table("vendite").update({"quantita": new_quantita}).eq("id", existing_record["id"]).execute()
                results.append(res.data[0])
            else:
                # Insert
                record = {
                    "data_vendita": data_vendita_iso,
                    "quantita": item.quantita,
                    "id_sede": auth_data["id_sede"],
                    "id_ricetta": id_ricetta,
                    "id_prodotto_commerciale": id_commerciale,
                }
                res = supabase.table("vendite").insert(record).execute()
                results.append(res.data[0])

        return {"message": f"{len(results)} vendite processate con successo.", "data": results}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/", status_code=status.HTTP_201_CREATED)
async def registra_vendita(data: VenditaCreate, auth_data = Depends(get_user_sede)):
    try:
        # Validazione base: deve esserci almeno uno dei due prodotti
        if not data.id_ricetta and not data.id_prodotto_commerciale:
            raise HTTPException(status_code=400, detail="Devi specificare quale prodotto è stato venduto.")

        # Controlla se esiste già una vendita per questo prodotto in questa data per questa sede
        query = supabase.table("vendite").select("*").eq("id_sede", auth_data["id_sede"]).eq("data_vendita", data.data_vendita.isoformat())
        if data.id_ricetta:
            query = query.eq("id_ricetta", data.id_ricetta)
        else:
            query = query.eq("id_prodotto_commerciale", data.id_prodotto_commerciale)
            
        existing = query.execute()
        
        if existing.data and len(existing.data) > 0:
            # Aggiorna la quantità
            existing_record = existing.data[0]
            new_quantita = existing_record["quantita"] + data.quantita
            res = supabase.table("vendite").update({"quantita": new_quantita}).eq("id", existing_record["id"]).execute()
            return res.data[0]
        else:
            # Crea nuova riga
            insert_data = data.model_dump(mode="json")
            insert_data["id_sede"] = auth_data["id_sede"]

            res = supabase.table("vendite").insert(insert_data).execute()
            return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/{id}")
async def aggiorna_vendita(id: int, data: VenditaUpdate, auth_data = Depends(get_user_sede)):
    try:
        update_data = data.model_dump(exclude_unset=True, mode="json")
        if not update_data:
            raise HTTPException(status_code=400, detail="Nessun dato da aggiornare.")
        
        res = supabase.table("vendite").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Vendita non trovata o non autorizzato.")
        return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/")
async def get_vendite(auth_data = Depends(get_user_sede)):
    # Recupera le vendite unendo i nomi delle ricette e dei prodotti commerciali per comodità visiva
    res = supabase.table("vendite").select(
        "*, ricette(nome_ricetta), anagrafica_rivendita(nome_articolo)"
    ).eq("id_sede", auth_data["id_sede"]).execute()
    return res.data

@router.delete("/{id}")
async def elimina_vendita(id: int, auth_data = Depends(get_user_sede)):
    res = supabase.table("vendite").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Vendita annullata"}

@router.post("/bulk-delete")
async def bulk_delete_vendite(data: VenditaBulkDelete, auth_data = Depends(get_user_sede)):
    if not data.ids:
        return {"message": "Nessun id fornito."}
    res = supabase.table("vendite").delete().in_("id", data.ids).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": f"Vendite annullate"}

@router.get("/export")
async def export_vendite(
    start_date: str, 
    end_date: str, 
    auth_data = Depends(get_user_sede)
):
    try:
        # Recupera le vendite nel range temporale unendo i nomi dei prodotti e i prezzi
        res = supabase.table("vendite").select(
            "data_vendita, quantita, ricette(nome_ricetta, prezzo_vendita_netto), anagrafica_rivendita(nome_articolo, prezzo_vendita_netto)"
        ).eq("id_sede", auth_data["id_sede"])\
         .gte("data_vendita", start_date)\
         .lte("data_vendita", end_date)\
         .order("data_vendita", desc=False).execute()

        data = res.data
        if not data:
            raise HTTPException(status_code=404, detail="Nessuna vendita trovata nel periodo selezionato.")

        # Flattening dei dati per pandas
        flat_data = []
        for item in data:
            nome_prodotto = "N/D"
            prezzo_unitario = 0.0
            
            # Gestione prodotto finito (Menu) - Ora prende da ricette
            if item.get("ricette") and item["ricette"]:
                nome_prodotto = item["ricette"].get("nome_ricetta", "N/D")
                prezzo_unitario = item["ricette"].get("prezzo_vendita_netto", 0.0)
            # Gestione prodotto commerciale (Rivendita)
            elif item.get("anagrafica_rivendita") and item["anagrafica_rivendita"]:
                nome_prodotto = item["anagrafica_rivendita"].get("nome_articolo", "N/D")
                prezzo_unitario = item["anagrafica_rivendita"].get("prezzo_vendita_netto", 0.0)

            quantita = item["quantita"]
            flat_data.append({
                "Data": item["data_vendita"],
                "Prodotto": nome_prodotto,
                "Quantità": quantita,
                "Prezzo Unitario (€)": prezzo_unitario,
                "Totale (€)": round(quantita * prezzo_unitario, 2)
            })

        df = pd.DataFrame(flat_data)
        
        # Conversione colonna Data in datetime per ordinamento sicuro
        df['Data'] = pd.to_datetime(df['Data'])
        df = df.sort_values(by=["Data", "Prodotto"])
        
        # Formattazione data per il file excel
        df['Data'] = df['Data'].dt.strftime('%d/%m/%Y')

        # Creazione file excel in memoria
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Vendite')
            
            worksheet = writer.sheets['Vendite']
            
            # Formattazione Intestazione: Grassetto
            header_font = Font(bold=True)
            for cell in worksheet[1]:
                cell.font = header_font

            # Ottimizzazione estetica: larghezza colonne automatica
            for i, col in enumerate(df.columns):
                column_len = df[col].astype(str).str.len().max()
                column_len = max(column_len, len(col)) + 4
                col_letter = chr(65 + i)
                worksheet.column_dimensions[col_letter].width = column_len

        output.seek(0)
        
        filename = f"esportazione_vendite_{start_date}_{end_date}.xlsx"
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Access-Control-Expose-Headers': 'Content-Disposition'
        }
        
        return StreamingResponse(
            output, 
            headers=headers, 
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        print(f"Excel Export Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

