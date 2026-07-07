import os
import json
import re
# Rimosso base64 perché non serve più con il nuovo SDK
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import List

# Importa genai e i types per gestire le immagini
from google import genai
from google.genai import types 

from database.config import Database
from utils.auth_utils import get_user_sede

router = APIRouter(prefix="/api/ai-scanner", tags=["AI Scanner"])
supabase = Database.get_client()

# Configura il client Gemini con la chiave API (Ottimo!)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "LA_TUA_CHIAVE") # Ricorda di non esporla
client = genai.Client(api_key=GEMINI_API_KEY)


def _build_prompt(menu_json: str) -> str:
    return f"""Sei un assistente IA esperto nell'estrazione dati da scontrini fiscali, pre-conti e comande di ristoranti.
Il tuo unico compito è leggere l'immagine fornita, estrarre i prodotti consumati e restituire i dati in un array JSON strutturato.

REGOLE UNIVERSALI DI ESTRAZIONE:
- DOCUMENTI MULTIPLI:
   - Se l'immagine contiene più scontrini separati, elaborali tutti insieme ma considera ognuno come scontrino/comanda singola assegnando a ciascuno un "id_documento" diverso (es. "Doc_1", "Doc_2"). Se ce n'è uno solo, usa "Doc_1".

1. IDENTIFICAZIONE PRODOTTI E COPERTO:
   - Estrai ogni singola voce ordinata dal cliente (piatti, bevande, dolci).
   - FAI ESTREMA ATTENZIONE AL "COPERTO": è una voce fondamentale in Italia, se lo vedi nello scontrino o nella comanda DEVI estrarlo sempre.
   - Ignora tutto ciò che non è un prodotto: subtotali, sconti, IVA, calcolo del resto, pagamenti elettronici, indirizzi o numeri di tavolo.

2. GESTIONE QUANTITÀ E LAYOUT DEGLI SCONTRINI (REGOLA FERREA):
   - I registratori di cassa stampano il moltiplicatore sempre sulla riga PRECEDENTE (SOPRA) al nome del prodotto.
   - REGOLA VISIVA INFALLIBILE: Se leggi una riga con il formato "Numero x Prezzo" (es. "3 x 25,00", "5 x 2,00"), quel "Numero" è la quantità del prodotto stampato ESATTAMENTE NELLA RIGA SOTTOSTANTE.
   - ESEMPI REALI CHE DEVI SEGUIRE ALLA LETTERA:
     CASO A:
     [Riga 1] 3 x 25,00
     [Riga 2] DONNA GIULIANA ROSATO     75,00
     -> Estrazione Corretta: Prodotto = "DONNA GIULIANA ROSATO", Quantita = 3. (Perché 3 x 25 fa 75).
     
     CASO B:
     [Riga 1] 5 x 2,00
     [Riga 2] Coperto                   10,00
     -> Estrazione Corretta: Prodotto = "Coperto", Quantita = 5. (Perché 5 x 2 fa 10).

   - DIVIETO ASSOLUTO: Non assegnare MAI il moltiplicatore al prodotto che si trova sulla riga precedente. Il flusso di lettura corretto è sempre: [Riga sopra = Moltiplicatore] -> [Riga sotto = Prodotto a cui si applica].
   - Se NON c'è una riga con un moltiplicatore sopra il prodotto, la quantità è SEMPRE 1 (es. "2P - CALABRIA DOCET" non ha moltiplicatori sopra, quindi la quantità è 1).
   - Anche il coperto potrebbe avere dei moltiplicatori (es. "3 x Coperto" o "3 x 2,00" ), applica le stesse regole. PRESTA ATTENZIONE AI MOLTIPLICATORI CHE SI RIFERISCONO AL COPERTO, SONO MOLTO FREQUENTI.

3. GESTIONE DELLA DATA:
   - Cerca la data stampata sul documento (formato "YYYY-MM-DD").
   - SE NON TROVI LA DATA (molto comune nelle comande interne o pre-conti tagliati), NON INVENTARLA MAI. Inserisci semplicemente il valore null nel JSON. Sarà l'utente del gestionale a inserirla manualmente in seguito.

4. MATCHING CON IL MENU E ASSOCIAZIONE MANUALE:
   - Confronta il nome del prodotto che hai letto con il menù del ristorante fornito qui sotto. 
   - Se trovi il prodotto nel menù, inserisci il suo "id".
   - SE NON RIESCI AD ASSOCIARLO con certezza (perché il nome è scritto male o non è in lista), inserisci null nel campo "id_prodotto_menu". L'IA non deve scartare il prodotto: ESTRAILO COMUNQUE, inserendo null nell'ID permetterai all'utente di fare l'associazione manuale.


MENU DEL RISTORANTE:
{menu_json}

OUTPUT RICHIESTO (Restituisci ESCLUSIVAMENTE l'array JSON piatto, senza testo prima o dopo e senza i backtick di formattazione Markdown):
[
  {{
    "id_documento": "Doc_1",
    "data_vendita": "YYYY-MM-DD" oppure null,
    "nome_rilevato": "Nome del prodotto estratto",
    "id_prodotto_menu": "id-del-prodotto" oppure null,
    "quantita": numero intero
  }}
]
"""


@router.post("/scan")
async def scan_receipts(
    files: List[UploadFile] = File(...),
    auth_data=Depends(get_user_sede),
):
    """
    Riceve immagini di scontrini, interroga il DB per il menù,
    chiama Gemini in modalità multimodale e restituisce solo i prodotti
    che hanno trovato corrispondenza nel menù (filtro di scarto).
    NON salva nulla nel DB.
    """
    # 1. Recupera l'intero menù della sede
    finiti_res = supabase.table("prodtti_finiti").select(
        "id, ricette(nome_ricetta)"
    ).eq("id_sede", auth_data["id_sede"]).execute()

    commerciali_res = supabase.table("articoli").select(
        "id, nome_articolo"
    ).eq("is_rivendita", True).eq("id_sede", auth_data["id_sede"]).execute()

    menu = []
    menu_lookup = {}
    for p in (finiti_res.data or []):
        nome = p.get("ricette", {}).get("nome_ricetta", "N/D") if p.get("ricette") else "N/D"
        item = {"id": str(p["id"]), "nome": nome, "tipo": "finito"}
        menu.append(item)
        menu_lookup[str(p["id"])] = item

    for p in (commerciali_res.data or []):
        item = {"id": str(p["id"]), "nome": p.get("nome_articolo", "N/D"), "tipo": "commerciale"}
        menu.append(item)
        menu_lookup[str(p["id"])] = item

    if not menu:
        raise HTTPException(
            status_code=400,
            detail="Nessun prodotto trovato nel tuo menù. Carica prima i prodotti nel gestionale."
        )

    menu_json_str = json.dumps(menu, ensure_ascii=False)
    prompt_text = _build_prompt(menu_json_str)

    # 2. Prepara l'array dei contenuti per Gemini (Testo + Immagini)
    contents = [prompt_text] # Iniziamo passando il testo del prompt

    for file in files:
        content = await file.read()
        mime_type = file.content_type or "image/jpeg"
        
        # NUOVA SINTASSI: Usiamo types.Part.from_bytes per iniettare l'immagine
        image_part = types.Part.from_bytes(
            data=content,
            mime_type=mime_type
        )
        contents.append(image_part)

    if len(contents) == 1: # Significa che c'è solo il prompt text, niente file
        raise HTTPException(status_code=400, detail="Nessuna immagine ricevuta.")

    # 3. Chiama Gemini
    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-image-preview',
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0)
        )
        raw_text = response.text.strip()
        print(f"Risposta grezza da Gemini: {raw_text}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Errore Gemini: {str(e)}")

    # 4. Parsing e Arricchimento Dati
    try:
        clean = re.sub(r"```(?:json)?", "", raw_text).strip().rstrip("`").strip()
        items = json.loads(clean)
        
        arricchiti = []
        # Utilizziamo menu_lookup creato sopra per trovare i nomi ufficiali e il tipo
        lower_lookup = {k.lower(): v for k, v in menu_lookup.items()}

        for item in items:
            raw_id = item.get("id_prodotto_menu")
            p_id = str(raw_id).lower() if raw_id else None
            
            if p_id and p_id in lower_lookup:
                official = lower_lookup[p_id]
                item["id_tipo"] = official["tipo"]
                item["nome_ufficiale"] = official["nome"]
                item["id_prodotto_menu"] = official["id"]
            else:
                item["id_prodotto_menu"] = None
                item["id_tipo"] = None
                item["nome_ufficiale"] = None
            
            arricchiti.append(item)
            
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Errore parsing: {str(e)}")

    input_tokens = response.usage_metadata.prompt_token_count
    output_tokens = response.usage_metadata.candidates_token_count
    total_tokens = response.usage_metadata.total_token_count

    print(f"Token usati - Input: {input_tokens}, Output: {output_tokens}, Totale: {total_tokens}")
    print(f"Risultati arricchiti: {json.dumps(arricchiti, ensure_ascii=False, indent=2)}")
    return {
        "risultati": arricchiti, 
        "totale_rilevati": len(arricchiti)
    }