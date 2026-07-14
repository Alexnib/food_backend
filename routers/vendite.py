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


        # 1. Raggruppa i dati in memoria per data_vendita e id prodotto
        # e separa vendite sospese dalle vendite valide
        vendite_sospese_to_insert = []
        valid_vendite_grouped = {} # chiave: (data_vendita_iso, id_ricetta, id_commerciale), valore: quantita
        
        for item in data.items:
            data_vendita_iso = item.data_vendita.isoformat()
            
            if item.id_tipo == "sospeso":
                vendite_sospese_to_insert.append({
                    "data_vendita": data_vendita_iso,
                    "quantita": item.quantita,
                    "id_sede": auth_data["id_sede"],
                    "nome_vendita": item.nome_vendita or "Sconosciuto"
                })
                continue
                
            id_ricetta = item.id_prodotto_menu if item.id_tipo == "finito" else None
            id_commerciale = item.id_prodotto_menu if item.id_tipo == "commerciale" else None
            
            key = (data_vendita_iso, id_ricetta, id_commerciale)
            if key in valid_vendite_grouped:
                valid_vendite_grouped[key] += item.quantita
            else:
                valid_vendite_grouped[key] = item.quantita
                
        results = []
        
        # 2. Inserisci le vendite sospese in bulk
        if vendite_sospese_to_insert:
            chunk_size = 500
            for i in range(0, len(vendite_sospese_to_insert), chunk_size):
                chunk = vendite_sospese_to_insert[i:i+chunk_size]
                res = supabase.table("vendite_sospese").insert(chunk).execute()
                results.extend(res.data)
                
        # 3. Gestisci le vendite valide con bulk upsert
        if valid_vendite_grouped:
            dates = [k[0] for k in valid_vendite_grouped.keys()]
            min_date = min(dates)
            max_date = max(dates)
            
            existing_sales = []
            page = 0
            page_size = 1000
            while True:
                res = supabase.table("vendite").select("*").eq("id_sede", auth_data["id_sede"]).gte("data_vendita", min_date).lte("data_vendita", max_date).range(page * page_size, (page + 1) * page_size - 1).execute()
                if not res.data:
                    break
                existing_sales.extend(res.data)
                if len(res.data) < page_size:
                    break
                page += 1
                
            existing_sales_dict = {}
            for sale in existing_sales:
                key = (sale["data_vendita"], sale.get("id_ricetta"), sale.get("id_prodotto_commerciale"))
                existing_sales_dict[key] = sale
                
            vendite_to_upsert = []
            for key, quantita in valid_vendite_grouped.items():
                data_vendita, id_ricetta, id_commerciale = key
                if key in existing_sales_dict:
                    sale = existing_sales_dict[key]
                    vendite_to_upsert.append({
                        "id": sale["id"],
                        "data_vendita": data_vendita,
                        "quantita": sale["quantita"] + quantita,
                        "id_sede": auth_data["id_sede"],
                        "id_ricetta": id_ricetta,
                        "id_prodotto_commerciale": id_commerciale
                    })
                else:
                    vendite_to_upsert.append({
                        "data_vendita": data_vendita,
                        "quantita": quantita,
                        "id_sede": auth_data["id_sede"],
                        "id_ricetta": id_ricetta,
                        "id_prodotto_commerciale": id_commerciale
                    })
                    
            if vendite_to_upsert:
                chunk_size = 500
                for i in range(0, len(vendite_to_upsert), chunk_size):
                    chunk = vendite_to_upsert[i:i+chunk_size]
                    res = supabase.table("vendite").upsert(chunk).execute()
                    if res.data:
                        results.extend(res.data)

        return {"message": f"{len(results)} voci processate (raggruppate) con successo.", "data": results}
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

@router.get("/summary")
async def get_vendite_summary(auth_data = Depends(get_user_sede)):
    """Restituisce il riepilogo delle vendite raggruppato per mese (YYYY-MM)."""
    res = supabase.table("vendite").select("data_vendita, quantita").eq("id_sede", auth_data["id_sede"]).execute()
    data = res.data
    
    summary = {}
    for item in data:
        # data_vendita è ISO 8601 (es: 2026-06-15)
        if not item.get("data_vendita"): continue
        month = item["data_vendita"][:7] # YYYY-MM
        if month not in summary:
            summary[month] = {"mese": month, "numero_operazioni": 0, "quantita_totale": 0}
        
        summary[month]["numero_operazioni"] += 1
        summary[month]["quantita_totale"] += item.get("quantita", 0)
        
    # Ordina per mese decrescente (i più recenti prima)
    result_list = sorted(list(summary.values()), key=lambda x: x["mese"], reverse=True)
    return result_list

from typing import Optional
import calendar

@router.get("/sospese")
async def get_vendite_sospese(auth_data = Depends(get_user_sede)):
    res = supabase.table("vendite_sospese").select("*").eq("id_sede", auth_data["id_sede"]).order("created_at", desc=True).execute()
    return res.data

@router.delete("/sospese/{id}")
async def delete_vendita_sospesa(id: str, auth_data = Depends(get_user_sede)):
    res = supabase.table("vendite_sospese").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendita sospesa non trovata o non autorizzato.")
    return {"message": "Vendita sospesa eliminata"}

@router.post("/sospese/{id}/resolve")
async def resolve_vendita_sospesa(id: str, data: VenditaSospesaResolve, auth_data = Depends(get_user_sede)):
    try:
        # Recupera la vendita sospesa
        res = supabase.table("vendite_sospese").select("*").eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Vendita sospesa non trovata.")
        
        sospesa = res.data[0]
        
        # Crea la vendita reale
        record = {
            "data_vendita": sospesa["data_vendita"],
            "quantita": sospesa["quantita"],
            "id_sede": auth_data["id_sede"],
            "id_ricetta": data.id_ricetta,
            "id_prodotto_commerciale": data.id_prodotto_commerciale,
        }
        
        # Inserisci in vendite
        supabase.table("vendite").insert(record).execute()
        
        # Elimina da vendite_sospese
        supabase.table("vendite_sospese").delete().eq("id", id).execute()
        
        return {"message": "Vendita risolta con successo"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/")
async def get_vendite(month: Optional[str] = None, auth_data = Depends(get_user_sede)):
    # Recupera le vendite unendo i nomi delle ricette e dei prodotti commerciali per comodità visiva
    query = supabase.table("vendite").select(
        "*, ricette(nome_ricetta), articoli(nome_articolo)"
    ).eq("id_sede", auth_data["id_sede"])
    
    if month:
        y, m = map(int, month.split('-'))
        last_day = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{last_day}"
        query = query.gte("data_vendita", start_date).lte("data_vendita", end_date)
        
    # Paginazione per superare il limite di 1000 righe di Supabase
    all_data = []
    page = 0
    page_size = 1000
    while True:
        res = query.range(page * page_size, (page + 1) * page_size - 1).execute()
        if not res.data:
            break
        all_data.extend(res.data)
        if len(res.data) < page_size:
            break
        page += 1
        
    return all_data

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
            "data_vendita, quantita, ricette(nome_ricetta, prezzo_vendita_netto), articoli(nome_articolo, prezzo_vendita_netto)"
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
            elif item.get("articoli") and item["articoli"]:
                nome_prodotto = item["articoli"].get("nome_articolo", "N/D")
                prezzo_unitario = item["articoli"].get("prezzo_vendita_netto", 0.0)

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

from fastapi import UploadFile, File
from fastapi.responses import StreamingResponse
from utils.ai_parser import parse_vendite_excel_with_ai_stream

@router.post("/import/upload")
async def upload_excel_vendite(file: UploadFile = File(...), auth_data=Depends(get_user_sede)):
    """
    Riceve il file Excel/CSV, lo legge e lo invia a Gemini per l'estrazione delle vendite.
    Ritorna uno stream NDJSON per aggiornamenti di progresso progressivi e il risultato finale.
    """
    content = await file.read()
    filename = file.filename
    
    async def event_generator():
        try:
            async for chunk in parse_vendite_excel_with_ai_stream(content, filename):
                yield chunk
        except Exception as e:
            import json
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")
