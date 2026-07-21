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
   - Ignora tutto ciò che non è un prodotto (non estrarlo come voce a sé): subtotali, sconti, calcolo del resto, pagamenti elettronici, indirizzi o numeri di tavolo. Le righe di riepilogo IVA fanno eccezione solo nel senso che vanno lette per ricavarne l'aliquota (vedi punto 3) ma non vanno mai estratte come un prodotto.

2. GESTIONE QUANTITÀ E LAYOUT DEGLI SCONTRINI (REGOLA FERREA):
   - I registratori di cassa stampano il moltiplicatore il più delle volte sulla riga PRECEDENTE (SOPRA) al nome del prodotto, ma su alcuni modelli di cassa/gestionale il moltiplicatore è stampato SOTTO, sulla riga SUCCESSIVA al prodotto. Non dare per scontata la posizione: individuala caso per caso osservando il layout reale del documento.
   - REGOLA VISIVA: Se leggi una riga con il formato "Numero x Prezzo" (es. "3 x 25,00", "5 x 2,00"), quel "Numero" è la quantità del prodotto a cui la riga è associata (sopra o sotto), e quel "Prezzo" è il prezzo unitario del prodotto (prezzo_singolo).
   - ESEMPI REALI CHE DEVI SEGUIRE ALLA LETTERA:
     CASO A (moltiplicatore sopra):
     [Riga 1] 3 x 25,00
     [Riga 2] DONNA GIULIANA ROSATO     75,00
     -> Estrazione Corretta: Prodotto = "DONNA GIULIANA ROSATO", Quantita = 3, prezzo_singolo = 25.00, prezzo_totale = 75.00. (Perché 3 x 25 fa 75).

     CASO B (moltiplicatore sopra, coperto):
     [Riga 1] 5 x 2,00
     [Riga 2] Coperto                   10,00
     -> Estrazione Corretta: Prodotto = "Coperto", Quantita = 5, prezzo_singolo = 2.00, prezzo_totale = 10.00. (Perché 5 x 2 fa 10).

     CASO C (moltiplicatore sotto):
     [Riga 1] ACQUA NATURALE 1L         6,00
     [Riga 2] 2 x 3,00
     -> Estrazione Corretta: Prodotto = "ACQUA NATURALE 1L", Quantita = 2, prezzo_singolo = 3.00, prezzo_totale = 6.00. (Perché 2 x 3 fa 6, e 6,00 coincide con l'importo già stampato sulla riga del prodotto).

   - PROVA DEL NOVE (VERIFICA OBBLIGATORIA): prima di finalizzare ogni riga, controlla sempre che quantita × prezzo_singolo = prezzo_totale (tollera differenze di pochi centesimi dovute ad arrotondamenti). Usa questo controllo per decidere se il moltiplicatore letto si riferisce al prodotto sopra o sotto, e per accorgerti di eventuali errori di lettura: se il conto non torna con l'ipotesi fatta, riconsidera quale riga è la quantità e quale il prodotto finché i numeri non tornano.
   - Se NON c'è alcuna riga con un moltiplicatore associata al prodotto (né sopra né sotto), la quantità è SEMPRE 1 (es. "2P - CALABRIA DOCET" senza moltiplicatori, quantità = 1). In questo caso l'unico numero stampato sulla riga del prodotto è sia prezzo_singolo che prezzo_totale (sono uguali, dato che quantita=1).
   - Anche il coperto potrebbe avere dei moltiplicatori (sopra o sotto, es. "3 x Coperto" o "3 x 2,00"), applica le stesse regole. PRESTA ATTENZIONE AI MOLTIPLICATORI CHE SI RIFERISCONO AL COPERTO, SONO MOLTO FREQUENTI.

3. GESTIONE DEL PREZZO E DELL'IVA:
   - 'prezzo_singolo': il prezzo di UNA unità del prodotto (quello nel moltiplicatore "Numero x Prezzo", o il prezzo stampato sulla riga stessa se non c'è moltiplicatore).
   - 'prezzo_totale': l'importo complessivo stampato su quella riga (quantità × prezzo unitario).
   - Se lo scontrino/comanda NON riporta alcun prezzo per una voce (capita in comande interne senza prezzi), lascia ENTRAMBI i campi a null: NON inventare né stimare un prezzo.
   - IMPORTANTE: i prezzi stampati su scontrini fiscali, pre-conti e comande sono SEMPRE prezzi LORDI (IVA già inclusa, è l'importo che il cliente paga). Estraili così come sono scritti: NON provare tu a scorporare l'IVA o a calcolare un prezzo netto, ci pensa il sistema a valle una volta note quantità e aliquota.
   - 'iva_percentuale': se il documento riporta esplicitamente un'aliquota IVA applicata (es. una dicitura "IVA 10%", "Aliquota 10,00%", oppure una tabella di riepilogo IVA a fondo scontrino con una o più aliquote per reparto), estrai quella percentuale come numero (es. 10, 22, 4). Se sul documento compare UNA SOLA aliquota valida per tutto lo scontrino, applicala a ogni riga. Se invece compaiono PIÙ aliquote diverse per reparti/categorie differenti e non riesci a determinare con certezza quale si applica a una specifica riga, lascia 'iva_percentuale' null per quella riga (il sistema userà comunque l'aliquota del prodotto già presente a listino). Se il documento non riporta alcuna indicazione di IVA, lascia sempre 'iva_percentuale' null.

4. GESTIONE DELLA DATA:
   - Cerca la data stampata sul documento (formato "YYYY-MM-DD").
   - SE NON TROVI LA DATA (molto comune nelle comande interne o pre-conti tagliati), NON INVENTARLA MAI. Inserisci semplicemente il valore null nel JSON. Sarà l'utente del gestionale a inserirla manualmente in seguito.

5. MATCHING CON IL MENU E ASSOCIAZIONE MANUALE:
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
    "quantita": numero intero,
    "prezzo_singolo": numero (prezzo unitario, LORDO come stampato) oppure null,
    "prezzo_totale": numero (importo di riga, LORDO come stampato) oppure null,
    "iva_percentuale": numero (aliquota IVA se indicata sullo scontrino) oppure null
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
    # 1. Recupera l'intero menù della sede (esclusi i prodotti eliminati: non ha
    # senso far associare l'IA a un prodotto non più in vendita).
    finiti_res = supabase.table("ricette").select(
        "id, nome_ricetta"
    ).eq("id_sede", auth_data["id_sede"]).eq("is_cancelled", False).execute()

    commerciali_res = supabase.table("articoli").select(
        "id, nome_articolo"
    ).eq("is_rivendita", True).eq("id_sede", auth_data["id_sede"]).eq("is_cancelled", False).execute()

    menu = []
    menu_lookup = {}
    for p in (finiti_res.data or []):
        item = {"id": str(p["id"]), "nome": p.get("nome_ricetta", "N/D"), "tipo": "finito"}
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