import os
import time
from google import genai
from google.genai import types
import pandas as pd
from typing import List, Optional
import io
from models.magazzino import ParsedResult

def parse_excel_with_ai(excel_file_bytes: bytes, filename: str, categorie_disponibili: list) -> str:
    """
    Legge il file excel o csv, lo converte in testo e lo invia a Gemini.
    Ritorna la stringa JSON validata.
    """
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(excel_file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(excel_file_bytes))
    except Exception as e:
        raise ValueError(f"Errore nella lettura del file: {str(e)}")

    cat_string = "\n".join([
        f"ID: {c.get('id')} - Nome: {c.get('nome_categoria')} - Tipo: {c.get('tipo_categoria', 'Sconosciuto')}" 
        for c in categorie_disponibili
    ])

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non configurata.")

    client = genai.Client(api_key=api_key)
    
    import json
    all_products = []
    chunk_size = 100

    for i in range(0, len(df), chunk_size):
        chunk_df = df.iloc[i:i+chunk_size]
        csv_string = chunk_df.to_csv(index=False)
        
        prompt = f"""
Sei un assistente esperto in ristorazione e magazzino in Italia.
Ti sto per fornire un file CSV (o estratto di Excel) caricato da un ristoratore.
Potrebbe essere disordinato, avere colonne senza nome o dati mancanti.

Il tuo compito è estrarre l'elenco dei prodotti e restituirlo come JSON rispettando il formato richiesto.
Per ogni prodotto:
1. 'nome_prodotto': Estrai o deduci il nome.
2. 'tipo': Valuta attentamente la natura del prodotto. Imposta "Materia Prima" per cibi/bevande usati per cucinare. Imposta "Rivendita" per prodotti venduti così come sono. Imposta "Entrambi" se il prodotto viene sia usato per preparazioni sia venduto direttamente al cliente (es. bibite, vini, birre). Imposta "Costo" per tutto ciò che NON è food/beverage ma è materiale di consumo, attrezzature, pulizia (es. bicchieri di plastica, cannucce, tovaglioli, detersivi, carta igienica).
3. 'unita_misura': Estrai o deduci l'unità di misura (kg, lt, pz).
4. 'iva_perc': Estrai l'IVA se c'è. Se l'IVA manca, applica l'aliquota italiana corretta in base al prodotto (solitamente 10% per alimenti/bevande in ristorazione, o 22%, o 4%).
5. 'costo_netto' e 'costo_lordo': Estraili. Se ne manca uno, calcolalo usando l'IVA. (Lordo = Netto * (1 + iva_perc/100)). Arrotonda sempre a 2 decimali.
6. 'id_categoria': Scegli l'ID della categoria più adatta tra questa lista fornita. Se nessuna si adatta, imposta null.

Lista Categorie Disponibili:
{cat_string}

Dati caricati:
```csv
{csv_string}
```

Ritorna ESCLUSIVAMENTE un JSON valido seguendo lo schema richiesto.
"""

        max_retries = 3
        chunk_result = None
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ParsedResult,
                        temperature=0.1
                    ),
                )
                chunk_result = response.text
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                raise ValueError(f"Errore AI dopo {max_retries} tentativi nel blocco {i}: {str(e)}")
        
        if chunk_result:
            try:
                parsed_chunk = json.loads(chunk_result)
                prodotti = parsed_chunk.get("prodotti", [])
                for p in prodotti:
                    if p.get("costo_netto") is not None:
                        p["costo_netto"] = round(float(p["costo_netto"]), 2)
                    if p.get("costo_lordo") is not None:
                        p["costo_lordo"] = round(float(p["costo_lordo"]), 2)
                all_products.extend(prodotti)
            except Exception as e:
                raise ValueError(f"Errore parsing JSON nel blocco {i}: {str(e)}")

    return json.dumps({"prodotti": all_products})
