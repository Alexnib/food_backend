from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.vendite import *
from utils.auth_utils import get_user_sede
from utils.numbers import round2
from utils.db_fetch import call_rpc_or_none, run_parallel, fetch_all_parallel
from fastapi.responses import StreamingResponse
import io
import time
import pandas as pd
from datetime import date
from openpyxl.styles import Font

router = APIRouter(prefix="/api/vendite", tags=["Vendite"])
supabase = Database.get_client()


def _get_listino_price(id_ricetta, id_prodotto_commerciale):
    """Prezzo di vendita netto ATTUALE dal listino (ricette o articoli), usato
    come fallback quando la vendita non porta con sé un prezzo esplicito
    (scontrino/excel senza colonna prezzo, inserimento manuale, ecc.)."""
    if id_ricetta:
        res = supabase.table("ricette").select("prezzo_vendita_netto").eq("id", id_ricetta).execute()
    elif id_prodotto_commerciale:
        res = supabase.table("articoli").select("prezzo_vendita_netto").eq("id", id_prodotto_commerciale).execute()
    else:
        return None
    return res.data[0].get("prezzo_vendita_netto") if res.data else None


def _get_listino_prices_batch(ids_ricette, ids_commerciali):
    """Versione batch di _get_listino_price, per non fare una query per riga
    durante un import massivo (AI scanner / excel)."""
    prezzi_ricette = {}
    if ids_ricette:
        res = supabase.table("ricette").select("id, prezzo_vendita_netto").in_("id", list(set(ids_ricette))).execute()
        prezzi_ricette = {r["id"]: r.get("prezzo_vendita_netto") for r in (res.data or [])}
    prezzi_articoli = {}
    if ids_commerciali:
        res = supabase.table("articoli").select("id, prezzo_vendita_netto").in_("id", list(set(ids_commerciali))).execute()
        prezzi_articoli = {a["id"]: a.get("prezzo_vendita_netto") for a in (res.data or [])}
    return prezzi_ricette, prezzi_articoli


def _get_iva_rates_batch(ids_ricette, ids_commerciali):
    """Aliquota IVA di vendita ATTUALE per prodotto (ricette.id_iva_vendita /
    articoli.id_iva_rivendita -> tabella iva), usata per scorporare un
    prezzo LORDO (scontrino/comanda/excel) in netto quando il documento
    sorgente non riporta esplicitamente l'aliquota applicata."""
    iva_ricette = {}
    iva_articoli = {}
    if not ids_ricette and not ids_commerciali:
        return iva_ricette, iva_articoli

    id_iva_by_ricetta = {}
    if ids_ricette:
        res = supabase.table("ricette").select("id, id_iva_vendita").in_("id", list(set(ids_ricette))).execute()
        id_iva_by_ricetta = {r["id"]: r.get("id_iva_vendita") for r in (res.data or [])}

    id_iva_by_articolo = {}
    if ids_commerciali:
        res = supabase.table("articoli").select("id, id_iva_rivendita").in_("id", list(set(ids_commerciali))).execute()
        id_iva_by_articolo = {a["id"]: a.get("id_iva_rivendita") for a in (res.data or [])}

    ids_iva = {v for v in list(id_iva_by_ricetta.values()) + list(id_iva_by_articolo.values()) if v is not None}
    percentuali_by_id_iva = {}
    if ids_iva:
        res = supabase.table("iva").select("id, iva").in_("id", list(ids_iva)).execute()
        percentuali_by_id_iva = {row["id"]: row.get("iva") for row in (res.data or [])}

    iva_ricette = {pid: percentuali_by_id_iva.get(id_iva) for pid, id_iva in id_iva_by_ricetta.items() if id_iva is not None}
    iva_articoli = {pid: percentuali_by_id_iva.get(id_iva) for pid, id_iva in id_iva_by_articolo.items() if id_iva is not None}
    return iva_ricette, iva_articoli


def _scorpora_iva(prezzo_lordo, iva_perc):
    """Converte un prezzo lordo (IVA inclusa) in netto. Se manca il prezzo o
    l'aliquota, ritorna il prezzo così com'era (nessuna conversione possibile)."""
    if prezzo_lordo is None or iva_perc is None:
        return prezzo_lordo
    return round(prezzo_lordo / (1 + iva_perc / 100), 2)


def _get_listino_lordo_netto_batch(ids_ricette, ids_commerciali):
    """Prezzo di vendita netto E lordo ATTUALI dal listino, per prodotto —
    usato per verificare/correggere la stima di prezzo_lordo dell'AI (vedi
    _correggi_prezzo_lordo): a differenza di _get_listino_prices_batch, qui
    serve anche il lordo, non solo il netto."""
    listino_ricette = {}
    if ids_ricette:
        res = supabase.table("ricette").select("id, prezzo_vendita_netto, prezzo_vendita_lordo").in_("id", list(set(ids_ricette))).execute()
        listino_ricette = {r["id"]: (r.get("prezzo_vendita_netto"), r.get("prezzo_vendita_lordo")) for r in (res.data or [])}
    listino_articoli = {}
    if ids_commerciali:
        res = supabase.table("articoli").select("id, prezzo_vendita_netto, prezzo_vendita_lordo").in_("id", list(set(ids_commerciali))).execute()
        listino_articoli = {a["id"]: (a.get("prezzo_vendita_netto"), a.get("prezzo_vendita_lordo")) for a in (res.data or [])}
    return listino_ricette, listino_articoli


def _correggi_prezzo_lordo(prezzo_grezzo, listino_netto, listino_lordo, ipotesi_ai):
    """
    L'AI stima se una colonna di prezzi è netta o lorda guardando se le cifre
    sono "tonde" — un'euristica che si rivela SBAGLIATA per i listini dove
    (come tipicamente in un locale) il prezzo tondo è quello mostrato al
    cliente (lordo, IVA inclusa) e il netto è il decimale scorporato: in quel
    caso l'AI scambia sistematicamente lordo per netto (visto in produzione:
    un intero mese di vendite salvato con l'IVA raddoppiata di fatto).

    Qui abbiamo un'informazione che l'AI non ha: il prodotto è già stato
    abbinato a una riga di catalogo, quindi conosciamo il suo VERO prezzo
    netto e lordo attuali. Se il prezzo grezzo estratto combacia chiaramente
    con uno dei due (e non con l'altro), quella è un'evidenza più affidabile
    della sola valutazione visiva dell'AI e la sostituisce. Se è ambiguo o
    non c'è un match netto, ci fidiamo della stima dell'AI (invariata).
    """
    if prezzo_grezzo is None:
        return ipotesi_ai
    TOLLERANZA = 0.03
    vicino_netto = listino_netto is not None and abs(prezzo_grezzo - listino_netto) < TOLLERANZA
    vicino_lordo = listino_lordo is not None and abs(prezzo_grezzo - listino_lordo) < TOLLERANZA
    if vicino_lordo and not vicino_netto:
        return True
    if vicino_netto and not vicino_lordo:
        return False
    return ipotesi_ai


def _get_costi_lordi_batch(ids_ricette, ids_commerciali):
    """Per un insieme di ricette/articoli, food cost unitario e aliquota IVA
    di vendita ATTUALI — le materie prime per congelare lordo e food cost su
    una vendita al momento in cui viene creata (vedi _snapshot_riga)."""
    food_cost_ricette, iva_id_ricette = {}, {}
    if ids_ricette:
        res = supabase.table("ricette").select("id, costo_ricetta_reale, id_iva_vendita").in_("id", list(set(ids_ricette))).execute()
        for r in res.data or []:
            food_cost_ricette[r["id"]] = r.get("costo_ricetta_reale")
            iva_id_ricette[r["id"]] = r.get("id_iva_vendita")

    food_cost_articoli, iva_id_articoli = {}, {}
    if ids_commerciali:
        res = supabase.table("articoli").select("id, prezzo_acquisto_netto, id_iva_rivendita").in_("id", list(set(ids_commerciali))).execute()
        for a in res.data or []:
            food_cost_articoli[a["id"]] = a.get("prezzo_acquisto_netto")
            iva_id_articoli[a["id"]] = a.get("id_iva_rivendita")

    ids_iva = {v for v in list(iva_id_ricette.values()) + list(iva_id_articoli.values()) if v is not None}
    perc_by_id_iva = {}
    if ids_iva:
        res = supabase.table("iva").select("id, iva").in_("id", list(ids_iva)).execute()
        perc_by_id_iva = {row["id"]: row.get("iva") for row in (res.data or [])}

    iva_perc_ricette = {pid: perc_by_id_iva.get(idiva) for pid, idiva in iva_id_ricette.items() if idiva is not None}
    iva_perc_articoli = {pid: perc_by_id_iva.get(idiva) for pid, idiva in iva_id_articoli.items() if idiva is not None}
    return food_cost_ricette, food_cost_articoli, iva_perc_ricette, iva_perc_articoli


def _snapshot_riga(id_ricetta, id_commerciale, prezzo_singolo, quantita, costi_lordi_batch):
    """Congela su una riga di vendita food cost e prezzo lordo, usando i
    valori ATTUALI di ricette/articoli passati in costi_lordi_batch (vedi
    _get_costi_lordi_batch). Unica implementazione di questo calcolo: la
    usano sia gli inserimenti singoli sia quelli bulk, per non rischiare due
    formule che nel tempo divergono silenziosamente."""
    food_cost_ricette, food_cost_articoli, iva_perc_ricette, iva_perc_articoli = costi_lordi_batch

    food_cost_unitario = food_cost_ricette.get(id_ricetta) if id_ricetta else (food_cost_articoli.get(id_commerciale) if id_commerciale else None)
    iva_perc = iva_perc_ricette.get(id_ricetta) if id_ricetta else (iva_perc_articoli.get(id_commerciale) if id_commerciale else None)

    out = {"food_cost_unitario": None, "food_cost_totale": None, "prezzo_singolo_lordo": None, "prezzo_totale_lordo": None}
    if food_cost_unitario is not None:
        out["food_cost_unitario"] = round2(food_cost_unitario)
        out["food_cost_totale"] = round(food_cost_unitario * (quantita or 0), 2)
    if prezzo_singolo is not None and iva_perc is not None:
        lordo_u = round2(prezzo_singolo * (1 + iva_perc / 100))
        out["prezzo_singolo_lordo"] = lordo_u
        out["prezzo_totale_lordo"] = round(lordo_u * (quantita or 0), 2)
    return out


def _snapshot_riga_singola(id_ricetta, id_commerciale, prezzo_singolo, quantita):
    """Versione comoda di _snapshot_riga per un solo prodotto (endpoint non
    bulk): recupera da sola i dati di ricette/articoli/iva necessari."""
    batch = _get_costi_lordi_batch(
        [id_ricetta] if id_ricetta else [],
        [id_commerciale] if id_commerciale else [],
    )
    return _snapshot_riga(id_ricetta, id_commerciale, prezzo_singolo, quantita, batch)


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
def registra_vendite_bulk(data: VenditaBulkPayload, auth_data=Depends(get_user_sede)):
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
        # chiave: (data_vendita_iso, id_ricetta, id_commerciale, price_bucket), valore: {"quantita", "prezzo_singolo"}
        valid_vendite_grouped = {}

        def _price_bucket(prezzo):
            # Il prezzo fa parte della chiave di raggruppamento: righe senza
            # prezzo esplicito si aggregano tra loro (prenderanno tutte lo
            # stesso fallback di listino più sotto), ma righe con un prezzo
            # ESPLICITO diverso restano SEPARATE — altrimenti uno sconto o un
            # cambio di prezzo infragiornaliero sullo stesso prodotto verrebbe
            # silenziosamente perso, con la quantità sommata sotto un unico
            # prezzo "vincente" e il resto scartato.
            return round(prezzo, 2) if prezzo is not None else None

        # 1a. Prima passata: classifica ogni riga e calcola il prezzo unitario
        # GREZZO (così com'è arrivato, ancora eventualmente lordo/IVA inclusa).
        # La conversione in netto avviene in un secondo momento perché richiede
        # di sapere già a quale prodotto (e quindi a quale aliquota IVA) la riga
        # è associata — cosa che per gli item "finito"/"commerciale" sappiamo
        # subito, ma che va comunque fatta in batch per non interrogare il DB
        # una volta per riga.
        pending = []
        for item in data.items:
            data_vendita_iso = item.data_vendita.isoformat()

            # Prezzo unitario di questa riga: quello esplicito (scontrino/excel), oppure
            # derivato dal totale di riga se solo quello è stato rilevato. Se nessuno dei
            # due è presente resta None e verrà recuperato dal listino più sotto.
            prezzo_singolo_item = round2(item.prezzo_singolo)
            if prezzo_singolo_item is None and item.prezzo_totale is not None and item.quantita:
                prezzo_singolo_item = round2(item.prezzo_totale / item.quantita)

            # Instradiamo esplicitamente solo i due casi con un prodotto riconosciuto.
            # Qualunque altro valore di id_tipo — "sospeso", None (l'AI non ha trovato
            # una corrispondenza, vedi routers/ai_scanner.py), o un valore imprevisto —
            # finisce tra le vendite sospese invece di diventare una "vendita fantasma":
            # una riga con quantità ma senza alcun prodotto collegato, invisibile al
            # calcolo dei ricavi.
            if item.id_tipo == "finito":
                id_ricetta = item.id_prodotto_menu
                id_commerciale = None
            elif item.id_tipo == "commerciale":
                id_ricetta = None
                id_commerciale = item.id_prodotto_menu
            else:
                id_ricetta = None
                id_commerciale = None

            pending.append({
                "item": item,
                "data_vendita_iso": data_vendita_iso,
                "prezzo_singolo": prezzo_singolo_item,
                "id_ricetta": id_ricetta,
                "id_commerciale": id_commerciale,
            })

        # 1a-bis. Verifica/correzione della stima "prezzo_lordo" dell'AI
        # contro il listino REALE del prodotto già abbinato (vedi
        # _correggi_prezzo_lordo) — per tutte le righe con un prezzo e un
        # prodotto riconosciuto, non solo quelle che l'AI ha segnato come
        # lorde: serve a correggere anche il caso opposto (l'AI ha detto
        # "netto" ma il prezzo è in realtà quello lordo di listino).
        ids_ricette_listino = {p["id_ricetta"] for p in pending if p["id_ricetta"]}
        ids_commerciali_listino = {p["id_commerciale"] for p in pending if p["id_commerciale"]}
        listino_ricette, listino_articoli = _get_listino_lordo_netto_batch(list(ids_ricette_listino), list(ids_commerciali_listino))

        for p in pending:
            netto_l, lordo_l = (
                listino_ricette.get(p["id_ricetta"]) if p["id_ricetta"]
                else listino_articoli.get(p["id_commerciale"]) if p["id_commerciale"]
                else (None, None)
            ) or (None, None)
            p["prezzo_lordo_corretto"] = _correggi_prezzo_lordo(p["prezzo_singolo"], netto_l, lordo_l, p["item"].prezzo_lordo)

        # 1b. Per le righe marcate come LORDE (scontrino/comanda, o excel con
        # prezzi riconosciuti come tali) e senza un'aliquota già nota dal
        # documento, recuperiamo in batch l'aliquota IVA di vendita del
        # prodotto associato, per poterle scorporare in netto.
        ids_ricette_iva = {
            p["id_ricetta"] for p in pending
            if p["prezzo_lordo_corretto"] and p["prezzo_singolo"] is not None
            and p["item"].iva_percentuale is None and p["id_ricetta"]
        }
        ids_commerciali_iva = {
            p["id_commerciale"] for p in pending
            if p["prezzo_lordo_corretto"] and p["prezzo_singolo"] is not None
            and p["item"].iva_percentuale is None and p["id_commerciale"]
        }
        iva_ricette, iva_articoli = _get_iva_rates_batch(list(ids_ricette_iva), list(ids_commerciali_iva))

        # 1c. Seconda passata: scorpora l'IVA dove serve (prezzo ora NETTO),
        # poi raggruppa le righe valide e separa quelle sospese, esattamente
        # come prima.
        for p in pending:
            item = p["item"]
            data_vendita_iso = p["data_vendita_iso"]
            id_ricetta = p["id_ricetta"]
            id_commerciale = p["id_commerciale"]
            prezzo_singolo_item = p["prezzo_singolo"]

            if p["prezzo_lordo_corretto"] and prezzo_singolo_item is not None:
                iva_perc = item.iva_percentuale
                if iva_perc is None:
                    iva_perc = iva_ricette.get(id_ricetta) if id_ricetta else iva_articoli.get(id_commerciale)
                prezzo_singolo_item = _scorpora_iva(prezzo_singolo_item, iva_perc)

            if item.id_tipo not in ("finito", "commerciale"):
                vendite_sospese_to_insert.append({
                    "data_vendita": data_vendita_iso,
                    "quantita": item.quantita,
                    "id_sede": auth_data["id_sede"],
                    "nome_vendita": item.nome_vendita or "Sconosciuto",
                    "prezzo_singolo": prezzo_singolo_item,
                    "prezzo_totale": round(prezzo_singolo_item * item.quantita, 2) if prezzo_singolo_item is not None else None,
                })
                continue

            key = (data_vendita_iso, id_ricetta, id_commerciale, _price_bucket(prezzo_singolo_item))
            if key in valid_vendite_grouped:
                # Il prezzo è già parte della chiave, quindi arriviamo qui solo
                # se questa riga condivide lo stesso prezzo (o la stessa assenza
                # di prezzo) del gruppo: sommare la quantità è sempre corretto.
                valid_vendite_grouped[key]["quantita"] += item.quantita
            else:
                valid_vendite_grouped[key] = {"quantita": item.quantita, "prezzo_singolo": prezzo_singolo_item}

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

            # Anche le vendite già salvate vengono cercate per (data, prodotto, PREZZO):
            # così un nuovo import si aggancia a una riga esistente solo se il prezzo
            # coincide, altrimenti finisce in una riga nuova invece di sovrascrivere
            # un prezzo diverso già registrato.
            existing_sales_dict = {}
            for sale in existing_sales:
                key = (sale["data_vendita"], sale.get("id_ricetta"), sale.get("id_prodotto_commerciale"), _price_bucket(sale.get("prezzo_singolo")))
                existing_sales_dict[key] = sale

            # Per i gruppi ancora senza prezzo (nessuna riga sorgente lo portava), lo
            # recuperiamo dal listino attuale — in batch, non una query per prodotto.
            ids_ricette_mancanti = {k[1] for k, g in valid_vendite_grouped.items() if g["prezzo_singolo"] is None and k[1]}
            ids_commerciali_mancanti = {k[2] for k, g in valid_vendite_grouped.items() if g["prezzo_singolo"] is None and k[2]}
            listino_ricette, listino_articoli = _get_listino_prices_batch(list(ids_ricette_mancanti), list(ids_commerciali_mancanti))
            for key, group in valid_vendite_grouped.items():
                if group["prezzo_singolo"] is None:
                    _, id_ricetta_k, id_commerciale_k, _ = key
                    group["prezzo_singolo"] = listino_ricette.get(id_ricetta_k) if id_ricetta_k else listino_articoli.get(id_commerciale_k)

            # Food cost e prezzo lordo ATTUALI di tutti i prodotti coinvolti, in
            # batch — per congelarli su ogni riga nuova o aggiornata (vedi
            # _snapshot_riga più sotto).
            ids_ricette_snapshot = {k[1] for k in valid_vendite_grouped.keys() if k[1]}
            ids_commerciali_snapshot = {k[2] for k in valid_vendite_grouped.keys() if k[2]}
            costi_lordi_batch = _get_costi_lordi_batch(list(ids_ricette_snapshot), list(ids_commerciali_snapshot))

            vendite_to_upsert = []
            for key, group in valid_vendite_grouped.items():
                data_vendita, id_ricetta, id_commerciale, _ = key
                quantita_da_aggiungere = group["quantita"]
                if key in existing_sales_dict:
                    # La chiave include già il prezzo, quindi se troviamo una riga
                    # esistente è garantito che condivida lo stesso prezzo (bucket) di
                    # questo gruppo: qui teniamo il valore della riga già salvata solo
                    # per precisione (non arrotondato) o come fallback se fosse null.
                    sale = existing_sales_dict[key]
                    new_quantita = sale["quantita"] + quantita_da_aggiungere
                    prezzo_singolo = round2(sale.get("prezzo_singolo") if sale.get("prezzo_singolo") is not None else group["prezzo_singolo"])

                    # Stesso principio del prezzo: se la riga esistente ha già uno
                    # snapshot congelato, lo teniamo (non lo sostituiamo col dato di
                    # oggi solo perché arriva altra quantità); ricalcoliamo solo i
                    # totali sulla nuova quantità complessiva.
                    fc_unitario = sale.get("food_cost_unitario")
                    pl_unitario = sale.get("prezzo_singolo_lordo")
                    if fc_unitario is None or pl_unitario is None:
                        fresh = _snapshot_riga(id_ricetta, id_commerciale, prezzo_singolo, new_quantita, costi_lordi_batch)
                        fc_unitario = fc_unitario if fc_unitario is not None else fresh["food_cost_unitario"]
                        pl_unitario = pl_unitario if pl_unitario is not None else fresh["prezzo_singolo_lordo"]

                    vendite_to_upsert.append({
                        "id": sale["id"],
                        "data_vendita": data_vendita,
                        "quantita": new_quantita,
                        "id_sede": auth_data["id_sede"],
                        "id_ricetta": id_ricetta,
                        "id_prodotto_commerciale": id_commerciale,
                        "prezzo_singolo": prezzo_singolo,
                        "prezzo_totale": round(new_quantita * prezzo_singolo, 2) if prezzo_singolo is not None else None,
                        "food_cost_unitario": fc_unitario,
                        "food_cost_totale": round(fc_unitario * new_quantita, 2) if fc_unitario is not None else None,
                        "prezzo_singolo_lordo": pl_unitario,
                        "prezzo_totale_lordo": round(pl_unitario * new_quantita, 2) if pl_unitario is not None else None,
                    })
                else:
                    prezzo_singolo = round2(group["prezzo_singolo"])
                    snap = _snapshot_riga(id_ricetta, id_commerciale, prezzo_singolo, quantita_da_aggiungere, costi_lordi_batch)
                    vendite_to_upsert.append({
                        "data_vendita": data_vendita,
                        "quantita": quantita_da_aggiungere,
                        "id_sede": auth_data["id_sede"],
                        "id_ricetta": id_ricetta,
                        "id_prodotto_commerciale": id_commerciale,
                        "prezzo_singolo": prezzo_singolo,
                        "prezzo_totale": round(quantita_da_aggiungere * prezzo_singolo, 2) if prezzo_singolo is not None else None,
                        **snap,
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
def registra_vendita(data: VenditaCreate, auth_data = Depends(get_user_sede)):
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

        # Prezzo unitario: quello passato esplicitamente, altrimenti quello attuale di listino.
        prezzo_singolo = round2(data.prezzo_singolo)
        if prezzo_singolo is None:
            prezzo_singolo = round2(_get_listino_price(data.id_ricetta, data.id_prodotto_commerciale))

        if existing.data and len(existing.data) > 0:
            # Aggiorna la quantità
            existing_record = existing.data[0]
            new_quantita = existing_record["quantita"] + data.quantita
            # Stesso prodotto, stesso giorno = stesso prezzo: se la riga già salvata
            # ce l'ha, prevale su quello appena determinato.
            prezzo_finale = round2(existing_record.get("prezzo_singolo") if existing_record.get("prezzo_singolo") is not None else prezzo_singolo)
            update_payload = {"quantita": new_quantita}
            if prezzo_finale is not None:
                update_payload["prezzo_singolo"] = prezzo_finale
                update_payload["prezzo_totale"] = round(new_quantita * prezzo_finale, 2)

            # Stesso principio del prezzo: se la riga ha già uno snapshot
            # congelato lo teniamo, altrimenti lo calcoliamo ora; in ogni caso
            # ribasiamo i totali sulla nuova quantità complessiva.
            fc_unitario = existing_record.get("food_cost_unitario")
            pl_unitario = existing_record.get("prezzo_singolo_lordo")
            if fc_unitario is None or pl_unitario is None:
                fresh = _snapshot_riga_singola(data.id_ricetta, data.id_prodotto_commerciale, prezzo_finale, new_quantita)
                fc_unitario = fc_unitario if fc_unitario is not None else fresh["food_cost_unitario"]
                pl_unitario = pl_unitario if pl_unitario is not None else fresh["prezzo_singolo_lordo"]
            if fc_unitario is not None:
                update_payload["food_cost_unitario"] = fc_unitario
                update_payload["food_cost_totale"] = round(fc_unitario * new_quantita, 2)
            if pl_unitario is not None:
                update_payload["prezzo_singolo_lordo"] = pl_unitario
                update_payload["prezzo_totale_lordo"] = round(pl_unitario * new_quantita, 2)

            res = supabase.table("vendite").update(update_payload).eq("id", existing_record["id"]).execute()
            return res.data[0]
        else:
            # Crea nuova riga
            insert_data = data.model_dump(mode="json")
            insert_data["id_sede"] = auth_data["id_sede"]
            insert_data["prezzo_singolo"] = prezzo_singolo
            if data.prezzo_totale is not None:
                insert_data["prezzo_totale"] = round2(data.prezzo_totale)
            elif prezzo_singolo is not None:
                insert_data["prezzo_totale"] = round(data.quantita * prezzo_singolo, 2)

            snap = _snapshot_riga_singola(data.id_ricetta, data.id_prodotto_commerciale, prezzo_singolo, data.quantita)
            insert_data.update(snap)

            res = supabase.table("vendite").insert(insert_data).execute()
            return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# NOTA: /bulk-prezzo deve restare PRIMA di /{id} qui sotto. Starlette prova le
# rotte nell'ordine di registrazione: /{id} (PUT) è un pattern a un solo
# segmento e "cattura" anche la stringa "bulk-prezzo" prima che si arrivi mai
# a valutare la rotta statica sottostante, con un 422 da FastAPI che prova a
# convertirla in int (bug preesistente, non introdotto da queste modifiche —
# la modifica prezzi in blocco non ha mai funzionato per questo).
@router.put("/bulk-prezzo")
def aggiorna_prezzo_bulk(data: VenditaBulkPrezzoUpdate, auth_data = Depends(get_user_sede)):
    """Applica lo stesso nuovo prezzo unitario a un insieme di vendite già
    registrate (selezionate dall'utente in /per-prodotto), ricalcolando il
    totale di riga in base alla quantità già salvata su ciascuna."""
    if not data.ids:
        return {"message": "Nessuna vendita selezionata."}

    nuovo_prezzo = round2(data.nuovo_prezzo_singolo)
    if nuovo_prezzo is None or nuovo_prezzo < 0:
        raise HTTPException(status_code=400, detail="Prezzo non valido.")

    res = supabase.table("vendite").select("id, quantita, id_ricetta, id_prodotto_commerciale").in_("id", data.ids).eq("id_sede", auth_data["id_sede"]).execute()
    righe = res.data or []

    # Il netto cambia: il lordo va ricalcolato sulla nuova base (aliquota IVA
    # ATTUALE del prodotto), altrimenti resterebbe legato al vecchio prezzo.
    # Il food cost non dipende dal prezzo di vendita e resta quello già
    # congelato sulla riga, non lo tocchiamo.
    ids_ricette = {r["id_ricetta"] for r in righe if r.get("id_ricetta")}
    ids_commerciali = {r["id_prodotto_commerciale"] for r in righe if r.get("id_prodotto_commerciale")}
    costi_lordi_batch = _get_costi_lordi_batch(list(ids_ricette), list(ids_commerciali))

    aggiornate = 0
    for riga in righe:
        snap = _snapshot_riga(riga.get("id_ricetta"), riga.get("id_prodotto_commerciale"), nuovo_prezzo, riga["quantita"], costi_lordi_batch)
        payload = {
            "prezzo_singolo": nuovo_prezzo,
            "prezzo_totale": round(riga["quantita"] * nuovo_prezzo, 2),
        }
        if snap["prezzo_singolo_lordo"] is not None:
            payload["prezzo_singolo_lordo"] = snap["prezzo_singolo_lordo"]
            payload["prezzo_totale_lordo"] = snap["prezzo_totale_lordo"]
        supabase.table("vendite").update(payload).eq("id", riga["id"]).execute()
        aggiornate += 1

    return {"message": f"{aggiornate} vendite aggiornate"}

@router.put("/{id}")
def aggiorna_vendita(id: int, data: VenditaUpdate, auth_data = Depends(get_user_sede)):
    try:
        update_data = data.model_dump(exclude_unset=True, mode="json")
        if not update_data:
            raise HTTPException(status_code=400, detail="Nessun dato da aggiornare.")

        # Arrotondiamo sempre a 2 decimali i prezzi passati esplicitamente dal client.
        if "prezzo_singolo" in update_data:
            update_data["prezzo_singolo"] = round2(update_data["prezzo_singolo"])
        if "prezzo_totale" in update_data:
            update_data["prezzo_totale"] = round2(update_data["prezzo_totale"])

        existing_res = supabase.table("vendite").select(
            "quantita, prezzo_singolo, id_ricetta, id_prodotto_commerciale, "
            "food_cost_unitario, prezzo_singolo_lordo"
        ).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        if not existing_res.data:
            raise HTTPException(status_code=404, detail="Vendita non trovata o non autorizzato.")
        existing = existing_res.data[0]

        # Se cambia la quantità ma non viene passato un nuovo prezzo totale,
        # lo ricalcoliamo dal prezzo unitario già salvato (o da quello nuovo, se passato).
        if "quantita" in update_data and "prezzo_totale" not in update_data:
            prezzo_singolo = update_data.get("prezzo_singolo", existing.get("prezzo_singolo"))
            if prezzo_singolo is not None:
                update_data["prezzo_totale"] = round(update_data["quantita"] * prezzo_singolo, 2)

        quantita_finale = update_data.get("quantita", existing.get("quantita"))
        id_ricetta_finale = update_data.get("id_ricetta", existing.get("id_ricetta"))
        id_commerciale_finale = update_data.get("id_prodotto_commerciale", existing.get("id_prodotto_commerciale"))

        if "id_ricetta" in update_data or "id_prodotto_commerciale" in update_data or "prezzo_singolo" in update_data:
            # Il prodotto o il prezzo netto sono cambiati: lo snapshot di lordo/
            # food-cost era legato a quelli, non ha più senso preservarlo — lo
            # ricalcoliamo da zero sui valori di catalogo ATTUALI.
            prezzo_singolo_finale = update_data.get("prezzo_singolo", existing.get("prezzo_singolo"))
            snap = _snapshot_riga_singola(id_ricetta_finale, id_commerciale_finale, prezzo_singolo_finale, quantita_finale)
            update_data.update(snap)
        elif "quantita" in update_data:
            # Cambia solo la quantità: l'unitario congelato resta quello, si
            # ribasano solo i totali.
            fc_unitario = existing.get("food_cost_unitario")
            pl_unitario = existing.get("prezzo_singolo_lordo")
            if fc_unitario is not None:
                update_data["food_cost_totale"] = round(fc_unitario * quantita_finale, 2)
            if pl_unitario is not None:
                update_data["prezzo_totale_lordo"] = round(pl_unitario * quantita_finale, 2)

        res = supabase.table("vendite").update(update_data).eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Vendita non trovata o non autorizzato.")
        return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/summary")
def get_vendite_summary(auth_data = Depends(get_user_sede)):
    """Restituisce il riepilogo delle vendite raggruppato per mese (YYYY-MM)."""
    id_sede = auth_data["id_sede"]

    # Percorso veloce: GROUP BY per mese direttamente in Postgres (vedi
    # sql/003_statistiche_rpc.sql) — viaggia solo una riga per mese, qualunque
    # sia il numero di vendite. Stessa forma e stesso ordinamento del fallback.
    rows = call_rpc_or_none("stat_vendite_summary", {"p_id_sede": id_sede}, order_cols=["-mese"])
    if rows is not None:
        return rows

    # Fallback (funzione SQL non ancora creata): scarica le date e conta in
    # Python, con le pagine oltre la prima recuperate in parallelo.
    def make_query(with_count):
        return supabase.table("vendite").select(
            "data_vendita, quantita", count="exact" if with_count else None
        ).eq("id_sede", id_sede)
    data = fetch_all_parallel(make_query)

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

@router.get("/per-prodotto")
def get_vendite_per_prodotto(
    id_ricetta: Optional[str] = None,
    id_prodotto_commerciale: Optional[str] = None,
    auth_data = Depends(get_user_sede)
):
    """Tutte le vendite già registrate di UN prodotto specifico, una riga per
    ogni combinazione data/prezzo distinta — usato dallo strumento di modifica
    prezzi in blocco: permette di vedere in un colpo solo tutti i giorni in cui
    quel prodotto è stato venduto, con il prezzo applicato quel giorno, e
    selezionarne alcuni (o tutti) per aggiornare il prezzo con /bulk-prezzo."""
    if not id_ricetta and not id_prodotto_commerciale:
        raise HTTPException(status_code=400, detail="Specifica un prodotto (ricetta o articolo).")

    query = supabase.table("vendite").select("*").eq("id_sede", auth_data["id_sede"])
    if id_ricetta:
        query = query.eq("id_ricetta", id_ricetta)
    else:
        query = query.eq("id_prodotto_commerciale", id_prodotto_commerciale)

    all_data = []
    page = 0
    page_size = 1000
    while True:
        res = query.order("data_vendita", desc=True).range(page * page_size, (page + 1) * page_size - 1).execute()
        if not res.data:
            break
        all_data.extend(res.data)
        if len(res.data) < page_size:
            break
        page += 1

    return all_data

@router.get("/sospese")
def get_vendite_sospese(auth_data = Depends(get_user_sede)):
    id_sede = auth_data["id_sede"]

    # Paginazione per superare il limite di righe di Supabase (stesso pattern di GET "/")
    data = []
    page = 0
    page_size = 1000
    while True:
        res = supabase.table("vendite_sospese").select("*").eq("id_sede", id_sede).order("created_at", desc=True).range(page * page_size, (page + 1) * page_size - 1).execute()
        if not res.data:
            break
        data.extend(res.data)
        if len(res.data) < page_size:
            break
        page += 1

    return data

@router.delete("/sospese/{id}")
def delete_vendita_sospesa(id: str, auth_data = Depends(get_user_sede)):
    res = supabase.table("vendite_sospese").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendita sospesa non trovata o non autorizzato.")
    return {"message": "Vendita sospesa eliminata"}

@router.post("/sospese/{id}/resolve")
def resolve_vendita_sospesa(id: str, data: VenditaSospesaResolve, auth_data = Depends(get_user_sede)):
    try:
        # Recupera la vendita sospesa
        res = supabase.table("vendite_sospese").select("*").eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Vendita sospesa non trovata.")
        
        sospesa = res.data[0]

        # Prezzo: se lo scontrino/excel l'aveva già rilevato sulla sospesa, resta quello;
        # altrimenti, ora che il prodotto è noto, lo recuperiamo dal listino attuale.
        prezzo_singolo = round2(sospesa.get("prezzo_singolo"))
        prezzo_totale = round2(sospesa.get("prezzo_totale"))
        if prezzo_singolo is None:
            prezzo_singolo = round2(_get_listino_price(data.id_ricetta, data.id_prodotto_commerciale))
        if prezzo_totale is None and prezzo_singolo is not None:
            prezzo_totale = round(sospesa["quantita"] * prezzo_singolo, 2)

        # Solo ora che il prodotto è noto possiamo congelare food cost e
        # prezzo lordo, esattamente come su una vendita registrata subito con
        # il prodotto già associato.
        snap = _snapshot_riga_singola(data.id_ricetta, data.id_prodotto_commerciale, prezzo_singolo, sospesa["quantita"])

        # Crea la vendita reale
        record = {
            "data_vendita": sospesa["data_vendita"],
            "quantita": sospesa["quantita"],
            "id_sede": auth_data["id_sede"],
            "id_ricetta": data.id_ricetta,
            "id_prodotto_commerciale": data.id_prodotto_commerciale,
            "prezzo_singolo": prezzo_singolo,
            "prezzo_totale": prezzo_totale,
            **snap,
        }

        # Inserisci in vendite
        supabase.table("vendite").insert(record).execute()
        
        # Elimina da vendite_sospese
        supabase.table("vendite_sospese").delete().eq("id", id).execute()
        
        return {"message": "Vendita risolta con successo"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# Aliquote IVA per prodotto della sede, con una piccola cache: servono solo a
# mostrare il prezzo lordo in elenco vendite e cambiano rarissimamente — senza
# cache erano 3 query in più su OGNI apertura della pagina Vendite.
_IVA_SEDE_CACHE: dict = {}
_IVA_SEDE_TTL = 60  # secondi


def _get_iva_rates_sede(id_sede: str):
    cached = _IVA_SEDE_CACHE.get(id_sede)
    if cached and (time.time() - cached[0]) < _IVA_SEDE_TTL:
        return cached[1], cached[2]

    res_r, res_a, res_iva = run_parallel(
        lambda: supabase.table("ricette").select("id, id_iva_vendita").eq("id_sede", id_sede).execute(),
        lambda: supabase.table("articoli").select("id, id_iva_rivendita").eq("id_sede", id_sede).execute(),
        lambda: supabase.table("iva").select("id, iva").execute(),
    )
    perc_by_id_iva = {row["id"]: row.get("iva") for row in (res_iva.data or [])}
    iva_ricette = {r["id"]: perc_by_id_iva.get(r.get("id_iva_vendita")) for r in (res_r.data or []) if r.get("id_iva_vendita") is not None}
    iva_articoli = {a["id"]: perc_by_id_iva.get(a.get("id_iva_rivendita")) for a in (res_a.data or []) if a.get("id_iva_rivendita") is not None}

    _IVA_SEDE_CACHE[id_sede] = (time.time(), iva_ricette, iva_articoli)
    return iva_ricette, iva_articoli


@router.get("/")
def get_vendite(month: Optional[str] = None, auth_data = Depends(get_user_sede)):
    # Recupera le vendite unendo i nomi delle ricette e dei prodotti commerciali
    # per comodità visiva. La prima pagina viaggia con count esatto, così le
    # pagine successive partono in parallelo invece che in sequenza; le aliquote
    # IVA (per il lordo) arrivano dalla cache di sede qui sopra.
    def make_query(with_count):
        q = supabase.table("vendite").select(
            "*, ricette(nome_ricetta), articoli(nome_articolo)",
            count="exact" if with_count else None,
        ).eq("id_sede", auth_data["id_sede"])
        if month:
            y, m = map(int, month.split('-'))
            last_day = calendar.monthrange(y, m)[1]
            q = q.gte("data_vendita", f"{month}-01").lte("data_vendita", f"{month}-{last_day}")
        return q

    all_data, (iva_ricette, iva_articoli) = run_parallel(
        lambda: fetch_all_parallel(make_query),
        lambda: _get_iva_rates_sede(auth_data["id_sede"]),
    )

    # Prezzo di vendita lordo: select("*") lo porta già con la riga se è stato
    # congelato al momento della vendita (vedi sql/004+005). Il calcolo al
    # volo dall'aliquota IVA ATTUALE resta solo come fallback per righe senza
    # snapshot (nessuna, dopo il backfill 005) — riflette l'aliquota di oggi,
    # non necessariamente quella in vigore al momento della vendita.

    for r in all_data:
        if r.get("prezzo_singolo_lordo") is not None and r.get("prezzo_totale_lordo") is not None:
            continue
        iva_perc = iva_ricette.get(r["id_ricetta"]) if r.get("id_ricetta") else iva_articoli.get(r.get("id_prodotto_commerciale"))
        if iva_perc is not None and r.get("prezzo_singolo") is not None:
            # Il totale lordo si deriva SEMPRE dall'unitario lordo già arrotondato
            # (quantita * unitario), mai da un arrotondamento indipendente sul
            # totale netto: stessa convenzione già in uso per il netto in tutto
            # il resto dell'app (vedi /bulk-prezzo), altrimenti le due cifre
            # possono divergere di un centesimo per via di arrotondamenti
            # indipendenti (es. 4.55 * 1.10 = 5.00 ma 54.60 * 1.10 / 12 = 5.005).
            r["prezzo_singolo_lordo"] = round2(r["prezzo_singolo"] * (1 + iva_perc / 100))
            r["prezzo_totale_lordo"] = round2(r["prezzo_singolo_lordo"] * (r.get("quantita") or 0))

    return all_data

@router.delete("/{id}")
def elimina_vendita(id: int, auth_data = Depends(get_user_sede)):
    res = supabase.table("vendite").delete().eq("id", id).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": "Vendita annullata"}

@router.post("/bulk-delete")
def bulk_delete_vendite(data: VenditaBulkDelete, auth_data = Depends(get_user_sede)):
    if not data.ids:
        return {"message": "Nessun id fornito."}
    res = supabase.table("vendite").delete().in_("id", data.ids).eq("id_sede", auth_data["id_sede"]).execute()
    return {"message": f"Vendite annullate"}

@router.get("/export")
def export_vendite(
    start_date: str, 
    end_date: str, 
    auth_data = Depends(get_user_sede)
):
    try:
        # Recupera le vendite nel range temporale unendo i nomi dei prodotti e i prezzi.
        # Paginazione per superare il limite di righe di Supabase (stesso pattern di GET "/"):
        # senza, un export su un range ampio potrebbe troncare silenziosamente il file Excel.
        # Il tiebreaker su "id" garantisce un ordinamento stabile tra una pagina e l'altra
        # anche quando più vendite condividono la stessa data_vendita.
        data = []
        page = 0
        page_size = 1000
        while True:
            res = supabase.table("vendite").select(
                "data_vendita, quantita, prezzo_singolo, prezzo_totale, ricette(nome_ricetta, prezzo_vendita_netto), articoli(nome_articolo, prezzo_vendita_netto)"
            ).eq("id_sede", auth_data["id_sede"])\
             .gte("data_vendita", start_date)\
             .lte("data_vendita", end_date)\
             .order("data_vendita", desc=False)\
             .order("id", desc=False)\
             .range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1

        if not data:
            raise HTTPException(status_code=404, detail="Nessuna vendita trovata nel periodo selezionato.")

        # Flattening dei dati per pandas
        flat_data = []
        for item in data:
            nome_prodotto = "N/D"
            prezzo_unitario_listino = 0.0

            # Gestione prodotto finito (Menu) - Ora prende da ricette
            if item.get("ricette") and item["ricette"]:
                nome_prodotto = item["ricette"].get("nome_ricetta", "N/D")
                prezzo_unitario_listino = item["ricette"].get("prezzo_vendita_netto", 0.0)
            # Gestione prodotto commerciale (Rivendita)
            elif item.get("articoli") and item["articoli"]:
                nome_prodotto = item["articoli"].get("nome_articolo", "N/D")
                prezzo_unitario_listino = item["articoli"].get("prezzo_vendita_netto", 0.0)

            quantita = item["quantita"]
            # Preferiamo il prezzo storico salvato sulla vendita (quello realmente
            # applicato quel giorno); per le vendite registrate prima di questa
            # funzionalità, che non ce l'hanno, ripieghiamo sul listino attuale.
            prezzo_unitario = item.get("prezzo_singolo") if item.get("prezzo_singolo") is not None else prezzo_unitario_listino
            totale = item.get("prezzo_totale") if item.get("prezzo_totale") is not None else round(quantita * prezzo_unitario, 2)
            flat_data.append({
                "Data": item["data_vendita"],
                "Prodotto": nome_prodotto,
                "Quantità": quantita,
                "Prezzo Unitario (€)": prezzo_unitario,
                "Totale (€)": totale
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
