from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.produzione import *
from utils.auth_utils import get_user_sede
from utils.numbers import round2

router = APIRouter(prefix="/api/produzione", tags=["Produzione e Ricette"])
supabase = Database.get_client()


def _costo_ingrediente(quantita_per_kg: float, perc_scarto: float, costo_unitario: float) -> float:
    """Costo di un ingrediente in ricetta, considerando lo scarto (Es. 20% scarto -> Resa 80% -> 0.8)."""
    resa = 1 - (perc_scarto / 100)
    quantita_effettiva = (quantita_per_kg / resa) if resa > 0 else quantita_per_kg
    return quantita_effettiva * costo_unitario


def _calcola_ingredienti_e_costo(id_sede: str, id_ricetta, ingredienti: list) -> tuple[list, float]:
    """
    Calcola il costo di ogni ingrediente e prepara le righe da inserire in
    ingredienti_ricetta. Un'UNICA query batch (.in_()) recupera i prezzi di
    TUTTE le materie prime coinvolte, invece di una query separata per ogni
    ingrediente: una ricetta da 15-20 ingredienti passava da 15-20 round-trip
    di rete sequenziali a 1 solo. Condivisa da create_ricetta e update_ricetta,
    che facevano lo stesso identico calcolo duplicato.
    """
    if not ingredienti:
        return [], 0.0

    ids_materie_prime = list({ing.id_materia_prima for ing in ingredienti})
    prezzi_res = supabase.table("articoli").select("id, prezzo_acquisto_netto")\
        .eq("id_sede", id_sede).in_("id", ids_materie_prime).execute()
    prezzi_map = {a["id"]: a.get("prezzo_acquisto_netto", 0) or 0 for a in (prezzi_res.data or [])}

    costo_totale_ricetta = 0.0
    ingredienti_da_inserire = []

    for ing in ingredienti:
        if ing.id_materia_prima not in prezzi_map:
            continue  # materia prima non trovata (o di un'altra sede): la saltiamo

        costo_totale_ricetta += _costo_ingrediente(ing.quantita_per_kg, ing.perc_scarto, prezzi_map[ing.id_materia_prima])

        # Prepariamo la riga per il database
        ingredienti_da_inserire.append({
            "id_ricetta": id_ricetta,
            "id_materia_prima": ing.id_materia_prima,
            "quantita_per_kg": ing.quantita_per_kg,
            "perc_scarto": ing.perc_scarto
        })

    return ingredienti_da_inserire, costo_totale_ricetta


def ricalcola_costo_ricette(id_ricetta_list: list) -> None:
    """
    Ricalcola e salva costo_ricetta_reale per le ricette indicate, usando i
    prezzi ATTUALI delle materie prime collegate.

    costo_ricetta_reale viene scritto solo qui e in create/update_ricetta: senza
    questa funzione, cambiare il prezzo di acquisto di una materia prima (in
    routers/magazzino.py) non si riflette mai sul food cost delle ricette che la
    usano, finché qualcuno non ri-salva manualmente ciascuna ricetta.
    """
    if not id_ricetta_list:
        return

    ricette_res = supabase.table("ricette").select(
        "id, ingredienti_ricetta(quantita_per_kg, perc_scarto, articoli(prezzo_acquisto_netto))"
    ).in_("id", id_ricetta_list).execute()

    for ricetta in (ricette_res.data or []):
        costo_totale = 0.0
        for ing in (ricetta.get("ingredienti_ricetta") or []):
            costo_unitario = (ing.get("articoli") or {}).get("prezzo_acquisto_netto", 0) or 0
            costo_totale += _costo_ingrediente(ing.get("quantita_per_kg", 0) or 0, ing.get("perc_scarto", 0) or 0, costo_unitario)

        supabase.table("ricette").update({"costo_ricetta_reale": round(costo_totale, 2)}).eq("id", ricetta["id"]).execute()


@router.post("/ricette", status_code=status.HTTP_201_CREATED)
def create_ricetta(data: RicettaCreate, auth_data = Depends(get_user_sede)):
    try:
        id_sede = auth_data["id_sede"]

        # 1. Creiamo il "contenitore" della ricetta (costo temporaneo 0)
        ricetta_insert = {
            "nome_ricetta": data.nome_ricetta,
            "descrizione_ricetta": data.descrizione_ricetta,
            "id_categoria_prodotto": data.id_categoria_prodotto,
            "id_sede": id_sede,
            "costo_ricetta_reale": 0.0,
            "prezzo_vendita_lordo": round2(data.prezzo_vendita_lordo),
            "prezzo_vendita_netto": round2(data.prezzo_vendita_netto),
            "id_iva_vendita": data.id_iva_vendita
        }
        res_ricetta = supabase.table("ricette").insert(ricetta_insert).execute()
        id_ricetta_creata = res_ricetta.data[0]["id"]

        # 2. Calcoliamo il costo di ogni ingrediente e li prepariamo per l'inserimento
        ingredienti_da_inserire, costo_totale_ricetta = _calcola_ingredienti_e_costo(
            id_sede, id_ricetta_creata, data.ingredienti
        )

        # Inseriamo tutti gli ingredienti nel DB in un colpo solo (Bulk Insert)
        if ingredienti_da_inserire:
            supabase.table("ingredienti_ricetta").insert(ingredienti_da_inserire).execute()

        # 3. Aggiorniamo la ricetta con il VERO Food Cost e i Margini calcolati
        costo_finale = round(costo_totale_ricetta, 2)

        supabase.table("ricette").update({
            "costo_ricetta_reale": costo_finale,
            "prezzo_vendita_lordo": round2(data.prezzo_vendita_lordo),
            "prezzo_vendita_netto": round2(data.prezzo_vendita_netto),
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
def get_ricette(auth_data = Depends(get_user_sede)):
    # Restituiamo le ricette e includiamo in automatico i loro ingredienti nidificati e categorie!
    # Paginazione interna per recuperare SEMPRE tutte le righe, anche oltre il cap di righe di
    # PostgREST/Supabase su una singola query (~1000), stesso pattern usato in routers/vendite.py.
    id_sede = auth_data["id_sede"]
    select_query = "*, categoria_prodotti(nome_categoria), ingredienti_ricetta(*, articoli(nome_articolo, unita_misura, prezzo_acquisto_netto))"

    tutte_le_ricette = []
    offset = 0
    page_size = 500  # batch più piccolo: ogni riga include ingredienti nidificati, payload più pesante
    while True:
        batch = supabase.table("ricette").select(select_query).eq("id_sede", id_sede).eq("is_cancelled", False).range(offset, offset + page_size - 1).execute()
        rows = batch.data or []
        tutte_le_ricette.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    return tutte_le_ricette

@router.put("/ricette/{id}")
def update_ricetta(id: str, data: RicettaCreate, auth_data = Depends(get_user_sede)):
    try:
        id_sede = auth_data["id_sede"]

        # 1. Elimina vecchi ingredienti
        supabase.table("ingredienti_ricetta").delete().eq("id_ricetta", id).execute()

        # 2. Ricalcola e reinserisci ingredienti
        ingredienti_da_inserire, costo_totale_ricetta = _calcola_ingredienti_e_costo(
            id_sede, id, data.ingredienti
        )

        if ingredienti_da_inserire:
            supabase.table("ingredienti_ricetta").insert(ingredienti_da_inserire).execute()

        costo_finale = round(costo_totale_ricetta, 2)

        # 3. Aggiorna dati ricetta
        update_data = {
            "nome_ricetta": data.nome_ricetta,
            "descrizione_ricetta": data.descrizione_ricetta,
            "id_categoria_prodotto": data.id_categoria_prodotto,
            "costo_ricetta_reale": costo_finale,
            "prezzo_vendita_lordo": round2(data.prezzo_vendita_lordo),
            "prezzo_vendita_netto": round2(data.prezzo_vendita_netto),
            "id_iva_vendita": data.id_iva_vendita
        }
        res = supabase.table("ricette").update(update_data).eq("id", id).eq("id_sede", id_sede).execute()

        return {"message": "Ricetta aggiornata", "id": id, "costo_ricetta_reale": costo_finale}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/ricette/{id}")
def delete_ricetta(id: str, auth_data = Depends(get_user_sede)):
    """Se la ricetta ha vendite registrate, eliminazione "soft" (is_cancelled=true):
    ricetta e ingredienti restano intatti a DB, sparisce solo dai picker di nuove
    vendite/ricette, mentre le vendite già registrate continuano a risalire al
    suo nome/prezzo storico.
    Se NON ha vendite, eliminazione reale: vengono rimossi anche gli ingredienti
    (ingredienti_ricetta) di questa ricetta, ma MAI gli articoli/materie prime che
    referenziava — quelli sono entità indipendenti e restano a catalogo."""
    try:
        id_sede = auth_data["id_sede"]

        vendite_res = supabase.table("vendite").select("id").eq("id_ricetta", id).limit(1).execute()
        if vendite_res.data:
            res = supabase.table("ricette").update({"is_cancelled": True}).eq("id", id).eq("id_sede", id_sede).execute()
            if not res.data:
                raise HTTPException(status_code=404, detail="Ricetta non trovata o non autorizzato.")
            return {"message": "Ricetta eliminata"}

        supabase.table("ingredienti_ricetta").delete().eq("id_ricetta", id).execute()
        res = supabase.table("ricette").delete().eq("id", id).eq("id_sede", id_sede).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Ricetta non trovata o non autorizzato.")
        return {"message": "Ricetta eliminata"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
