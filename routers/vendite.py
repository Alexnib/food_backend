from fastapi import APIRouter, Depends, HTTPException, status
from database.config import Database
from models.vendite import *
from utils.auth_utils import get_user_sede
from utils.numbers import round2
from utils.db_fetch import call_rpc_or_none, run_parallel, fetch_all_parallel
from fastapi.responses import StreamingResponse
import io
import time
import logging
import pandas as pd
from datetime import date
from openpyxl.styles import Font

logger = logging.getLogger(__name__)

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
                # Totale di riga così come scritto nel file, PRIMA di derivarlo
                # dal prezzo unitario: preservato per non perdere precisione
                # quando quantità non divide esattamente il totale (es. 39€ per
                # 35 unità -> unitario 1,11 che moltiplicato per 35 non ritorna
                # a 39 esatti). Vedi uso in 1c.
                "prezzo_totale_raw": round2(item.prezzo_totale) if item.prezzo_totale is not None else None,
                "id_ricetta": id_ricetta,
                "id_commerciale": id_commerciale,
            })

        # 1b. Per le righe marcate come LORDE (scontrino/comanda, o excel con
        # prezzi riconosciuti come tali) e senza un'aliquota già nota dal
        # documento, recuperiamo in batch l'aliquota IVA di vendita del
        # prodotto associato, per poterle scorporare in netto.
        ids_ricette_iva = {
            p["id_ricetta"] for p in pending
            if p["item"].prezzo_lordo and p["prezzo_singolo"] is not None
            and p["item"].iva_percentuale is None and p["id_ricetta"]
        }
        ids_commerciali_iva = {
            p["id_commerciale"] for p in pending
            if p["item"].prezzo_lordo and p["prezzo_singolo"] is not None
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
            prezzo_totale_raw = p["prezzo_totale_raw"]

            # Una vendita sospesa non ha (ancora) un prodotto associato, quindi
            # nessuna aliquota IVA nota con cui scorporare un netto attendibile
            # — prima si tentava comunque lo scorporo (quasi sempre con
            # iva_perc None, dato che id_ricetta/id_commerciale sono sempre
            # None qui), e nel fallback si ricostruiva il totale come
            # unitario*quantità invece di preservare il totale ESATTO letto
            # dalla fonte, perdendo precisione ogni volta che il file dava un
            # totale diretto invece che solo un prezzo unitario. Si salva
            # sempre il valore così com'è dalla fonte (lordo, per scanner/
            # import Excel vendite: prezzo_lordo è sempre true lì) — lo
            # scorporo avviene solo più avanti, quando l'utente risolve la
            # sospesa assegnandole un prodotto reale con aliquota nota.
            if item.id_tipo not in ("finito", "commerciale"):
                prezzo_singolo_sospeso = prezzo_singolo_item
                prezzo_totale_sospeso = prezzo_totale_raw if prezzo_totale_raw is not None else (
                    round(prezzo_singolo_sospeso * item.quantita, 2) if prezzo_singolo_sospeso is not None else None
                )
                vendite_sospese_to_insert.append({
                    "data_vendita": data_vendita_iso,
                    "quantita": item.quantita,
                    "id_sede": auth_data["id_sede"],
                    "nome_vendita": item.nome_vendita or "Sconosciuto",
                    "prezzo_singolo": prezzo_singolo_sospeso,
                    "prezzo_totale": prezzo_totale_sospeso,
                })
                continue

            iva_perc = None
            if item.prezzo_lordo and (prezzo_singolo_item is not None or prezzo_totale_raw is not None):
                iva_perc = item.iva_percentuale
                if iva_perc is None:
                    iva_perc = iva_ricette.get(id_ricetta) if id_ricetta else iva_articoli.get(id_commerciale)

            # Totale ESATTO di questa riga (netto e lordo), quando il file dava
            # un totale esplicito: deriva direttamente da quello, non da
            # unitario*quantità, per non perdere i centesimi di cui sopra. Se
            # il file dava solo un prezzo unitario (nessun totale proprio),
            # resta None: il totale del gruppo si baserà su unitario*quantità
            # come sempre.
            totale_netto_riga = None
            totale_lordo_riga = None
            if prezzo_totale_raw is not None:
                if item.prezzo_lordo:
                    totale_lordo_riga = prezzo_totale_raw
                    totale_netto_riga = _scorpora_iva(prezzo_totale_raw, iva_perc) if iva_perc is not None else None
                else:
                    totale_netto_riga = prezzo_totale_raw
                    totale_lordo_riga = round(prezzo_totale_raw * (1 + iva_perc / 100), 2) if iva_perc is not None else None

            if item.prezzo_lordo and prezzo_singolo_item is not None:
                prezzo_singolo_item = _scorpora_iva(prezzo_singolo_item, iva_perc)

            key = (data_vendita_iso, id_ricetta, id_commerciale, _price_bucket(prezzo_singolo_item))
            if key in valid_vendite_grouped:
                # Il prezzo è già parte della chiave, quindi arriviamo qui solo
                # se questa riga condivide lo stesso prezzo (o la stessa assenza
                # di prezzo) del gruppo: sommare la quantità è sempre corretto.
                g = valid_vendite_grouped[key]
                g["quantita"] += item.quantita
                if g["totale_netto_esatto"] is not None and totale_netto_riga is not None:
                    g["totale_netto_esatto"] += totale_netto_riga
                    g["totale_lordo_esatto"] += totale_lordo_riga
                else:
                    # Anche una sola riga del gruppo senza totale esatto proprio
                    # (solo prezzo unitario) fa perdere la precisione a tutto il
                    # gruppo: si ricade su unitario*quantità come prima.
                    g["totale_netto_esatto"] = None
                    g["totale_lordo_esatto"] = None
            else:
                valid_vendite_grouped[key] = {
                    "quantita": item.quantita,
                    "prezzo_singolo": prezzo_singolo_item,
                    "totale_netto_esatto": totale_netto_riga,
                    "totale_lordo_esatto": totale_lordo_riga,
                }

        results = []

        # 2. Inserisci le vendite sospese in bulk
        if vendite_sospese_to_insert:
            chunk_size = 500
            for i in range(0, len(vendite_sospese_to_insert), chunk_size):
                chunk = vendite_sospese_to_insert[i:i+chunk_size]
                res = supabase.table("vendite_sospese").insert(chunk).execute()
                results.extend(res.data)

        # 3. Gestisci le vendite valide con upsert ATOMICO via RPC (vedi
        # sql/013_vendite_bulk_upsert_rpc.sql). La vecchia versione leggeva le
        # vendite esistenti con una SELECT separata e decideva in Python se
        # una riga esisteva già PRIMA di scrivere: tra quella lettura e la
        # scrittura successiva (due chiamate HTTP/transazioni distinte, nessuna
        # connessione persistente via PostgREST) due richieste concorrenti
        # sulla stessa chiave (stesso prodotto/giorno/prezzo) potevano
        # entrambe decidere "nessuna riga esistente" e sovrascriversi a
        # vicenda, perdendo silenziosamente quantità (lost update). La
        # funzione SQL fa l'intera sequenza "esiste già? somma o ricalcola?
        # scrivi" in un'unica transazione per gruppo, con Postgres stesso a
        # serializzare le scritture concorrenti tramite ON CONFLICT contro un
        # indice UNIQUE reale — non più il codice Python.
        if valid_vendite_grouped:
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

            payload_groups = []
            for key, group in valid_vendite_grouped.items():
                data_vendita, id_ricetta, id_commerciale, _ = key
                prezzo_singolo = round2(group["prezzo_singolo"])
                # food_cost_unitario e prezzo_singolo_lordo NON dipendono dalla
                # quantità finale (che si conosce solo dentro la funzione SQL,
                # dopo aver visto se la riga esiste già): calcolabili sempre
                # qui, a prescindere che il gruppo finisca per essere un
                # insert o un update.
                unit_snap = _snapshot_riga(id_ricetta, id_commerciale, prezzo_singolo, group["quantita"], costi_lordi_batch)
                payload_groups.append({
                    "data_vendita": data_vendita,
                    "id_ricetta": id_ricetta,
                    "id_prodotto_commerciale": id_commerciale,
                    "delta_quantita": group["quantita"],
                    "prezzo_singolo": prezzo_singolo,
                    "totale_netto_esatto": group["totale_netto_esatto"],
                    "totale_lordo_esatto": group["totale_lordo_esatto"],
                    "food_cost_unitario": unit_snap["food_cost_unitario"],
                    "prezzo_singolo_lordo": unit_snap["prezzo_singolo_lordo"],
                })

            if payload_groups:
                chunk_size = 500
                for i in range(0, len(payload_groups), chunk_size):
                    chunk = payload_groups[i:i + chunk_size]
                    # Niente call_rpc_or_none qui: se la funzione manca ancora
                    # sul DB (sql/013 non ancora eseguito), l'errore deve
                    # emergere chiaro tramite l'except sotto — un fallback
                    # silenzioso ricadrebbe esattamente sul percorso racy che
                    # questa modifica rimuove.
                    res = supabase.rpc("upsert_vendite_bulk", {
                        "p_id_sede": auth_data["id_sede"],
                        "p_groups": chunk,
                    }).execute()
                    if res.data:
                        results.extend(res.data)

        return {"message": f"{len(results)} voci processate (raggruppate) con successo.", "data": results}
    except HTTPException:
        # Già un errore "di validazione" deliberato (es. "Nessun item da
        # salvare."): passa invariato, non va ricatalogato come 400 generico
        # né confuso con un bug reale nel ramo sotto.
        raise
    except Exception as e:
        # Prima di questa modifica QUALSIASI eccezione qui (compreso un vero
        # bug — AttributeError, KeyError su una risposta inattesa dell'RPC,
        # ecc.) diventava un 400 col testo grezzo dell'eccezione: indistinguibile,
        # per l'utente e per un eventuale monitoraggio, da un errore di
        # validazione. Logghiamo sempre lo stack completo; solo un errore
        # riconducibile a un problema di dati (RPC mancante, valori non
        # validi) resta un 400 — il resto è un 500, segnale che c'è un bug da
        # investigare, non "colpa" di chi ha caricato il file.
        logger.exception("registra_vendite_bulk: errore inatteso")
        if isinstance(e, (ValueError, TypeError)) or getattr(e, "code", None) == "PGRST202":
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=500, detail="Errore interno durante il salvataggio delle vendite. Riprova più tardi.")

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
    """Applica un nuovo prezzo LORDO (IVA inclusa, quello che l'utente
    conosce/vede davvero) a un insieme di vendite già registrate (selezionate
    dall'utente in /per-prodotto), in una delle due modalità (vedi
    VenditaBulkPrezzoUpdate): stesso prezzo UNITARIO per ogni riga (il totale
    segue dalla quantità di ciascuna) oppure stesso TOTALE per ogni riga (il
    prezzo unitario si ricava dividendo per la quantità di ciascuna). Il
    netto si ottiene scorporando l'aliquota IVA ATTUALE del prodotto."""
    if not data.ids:
        return {"message": "Nessuna vendita selezionata."}

    usa_totale = data.nuovo_totale_lordo is not None
    valore_lordo = round2(data.nuovo_totale_lordo if usa_totale else data.nuovo_prezzo_singolo_lordo)
    if valore_lordo is None or valore_lordo < 0:
        raise HTTPException(status_code=400, detail="Prezzo non valido.")

    res = supabase.table("vendite").select("id, quantita, id_ricetta, id_prodotto_commerciale").in_("id", data.ids).eq("id_sede", auth_data["id_sede"]).execute()
    righe = res.data or []

    # Aliquota IVA ATTUALE per prodotto, per scorporare il lordo appena
    # inserito in un netto coerente. Il food cost non dipende dal prezzo di
    # vendita e resta quello già congelato sulla riga, non lo tocchiamo.
    ids_ricette = {r["id_ricetta"] for r in righe if r.get("id_ricetta")}
    ids_commerciali = {r["id_prodotto_commerciale"] for r in righe if r.get("id_prodotto_commerciale")}
    iva_ricette, iva_articoli = _get_iva_rates_batch(list(ids_ricette), list(ids_commerciali))

    aggiornate = 0
    for riga in righe:
        iva_perc = iva_ricette.get(riga.get("id_ricetta")) if riga.get("id_ricetta") else iva_articoli.get(riga.get("id_prodotto_commerciale"))
        quantita = riga["quantita"] or 0

        if usa_totale:
            # Il valore AUTORITATIVO qui è il totale: lo scorporiamo
            # direttamente (non unitario*quantità) per non perdere i
            # centesimi, stesso principio già in uso per gli import bulk
            # (vedi registra_vendite_bulk). L'unitario è solo derivato, per
            # coerenza visiva altrove nell'app.
            totale_lordo_riga = valore_lordo
            prezzo_lordo_riga = round2(valore_lordo / quantita) if quantita else valore_lordo
        else:
            # Qui l'AUTORITATIVO è l'unitario: il totale segue da
            # quantità * unitario, come già faceva questo endpoint.
            prezzo_lordo_riga = valore_lordo
            totale_lordo_riga = round(quantita * valore_lordo, 2)

        prezzo_netto = _scorpora_iva(prezzo_lordo_riga, iva_perc)
        if prezzo_netto is None:
            prezzo_netto = prezzo_lordo_riga  # aliquota IVA sconosciuta: nessuno scorporo possibile

        totale_netto = _scorpora_iva(totale_lordo_riga, iva_perc)
        if totale_netto is None:
            totale_netto = totale_lordo_riga

        payload = {
            "prezzo_singolo": prezzo_netto,
            "prezzo_totale": totale_netto,
            "prezzo_singolo_lordo": prezzo_lordo_riga,
            "prezzo_totale_lordo": totale_lordo_riga,
        }
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

        # Quantità e data: usa le correzioni dell'utente se presenti (righe
        # importate da Excel spesso hanno l'una o l'altra sbagliata), altrimenti
        # quelle già salvate sulla riga sospesa.
        quantita_finale = data.quantita if data.quantita is not None else sospesa["quantita"]
        data_vendita_finale = data.data_vendita.isoformat() if data.data_vendita is not None else sospesa["data_vendita"]

        # Aliquota IVA del prodotto ora noto: serve per scorporare in netto sia
        # che il lordo venga dalla riga importata sia che l'utente lo abbia
        # corretto qui sotto.
        iva_ricette, iva_articoli = _get_iva_rates_batch(
            [data.id_ricetta] if data.id_ricetta else [],
            [data.id_prodotto_commerciale] if data.id_prodotto_commerciale else [],
        )
        iva_perc = iva_ricette.get(data.id_ricetta) if data.id_ricetta else iva_articoli.get(data.id_prodotto_commerciale)

        if data.prezzo_totale_lordo is not None:
            # L'utente ha corretto il totale LORDO: è questo il valore
            # autoritativo, l'unitario si ricava dividendolo per la quantità
            # finale (stesso principio di aggiorna_prezzo_bulk in modalità totale).
            totale_lordo = round2(data.prezzo_totale_lordo)
            prezzo_singolo_lordo = round2(totale_lordo / quantita_finale) if quantita_finale else totale_lordo
        else:
            # Il prezzo salvato su una vendita sospesa è SEMPRE lordo grezzo,
            # mai netto: senza un prodotto abbinato non c'era un'aliquota IVA
            # nota con cui scorporarlo al momento dell'inserimento (vedi
            # registra_vendite_bulk).
            prezzo_singolo_lordo = round2(sospesa.get("prezzo_singolo"))
            totale_lordo = round(quantita_finale * prezzo_singolo_lordo, 2) if prezzo_singolo_lordo is not None else None

        # Ora che il prodotto è noto, scorporiamo qui, per la prima volta, i
        # valori in netto (il totale si scorpora direttamente, non da
        # unitario*quantità, per non perdere i centesimi — stesso principio
        # già in uso per gli import bulk e per aggiorna_prezzo_bulk).
        if prezzo_singolo_lordo is not None:
            prezzo_singolo = _scorpora_iva(prezzo_singolo_lordo, iva_perc)
            if prezzo_singolo is None:
                prezzo_singolo = prezzo_singolo_lordo
        else:
            # Nessun prezzo rilevato sullo scontrino/excel: usiamo il listino
            # netto attuale del prodotto ora noto (già netto, nessuno scorporo).
            prezzo_singolo = round2(_get_listino_price(data.id_ricetta, data.id_prodotto_commerciale))

        if totale_lordo is not None:
            prezzo_totale = _scorpora_iva(totale_lordo, iva_perc)
            if prezzo_totale is None:
                prezzo_totale = totale_lordo
        else:
            prezzo_totale = round(quantita_finale * prezzo_singolo, 2) if prezzo_singolo is not None else None

        # Food cost: dipende solo dal prodotto (costo attuale), non dal
        # prezzo di vendita corretto qui sopra — lo congeliamo comunque sulla
        # riga, esattamente come su una vendita registrata subito col
        # prodotto già associato.
        food_cost_ricette, food_cost_articoli, _, _ = _get_costi_lordi_batch(
            [data.id_ricetta] if data.id_ricetta else [],
            [data.id_prodotto_commerciale] if data.id_prodotto_commerciale else [],
        )
        food_cost_unitario = food_cost_ricette.get(data.id_ricetta) if data.id_ricetta else food_cost_articoli.get(data.id_prodotto_commerciale)

        # Crea la vendita reale
        record = {
            "data_vendita": data_vendita_finale,
            "quantita": quantita_finale,
            "id_sede": auth_data["id_sede"],
            "id_ricetta": data.id_ricetta,
            "id_prodotto_commerciale": data.id_prodotto_commerciale,
            "prezzo_singolo": prezzo_singolo,
            "prezzo_totale": prezzo_totale,
        }
        if prezzo_singolo_lordo is not None:
            record["prezzo_singolo_lordo"] = prezzo_singolo_lordo
            record["prezzo_totale_lordo"] = totale_lordo
        if food_cost_unitario is not None:
            record["food_cost_unitario"] = round2(food_cost_unitario)
            record["food_cost_totale"] = round(food_cost_unitario * quantita_finale, 2)

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
from utils.ai_usage import check_and_log_ai_usage

@router.post("/import/upload")
async def upload_excel_vendite(file: UploadFile = File(...), auth_data=Depends(get_user_sede)):
    """
    Riceve il file Excel/CSV, lo legge e lo invia a Gemini per l'estrazione delle vendite.
    Ritorna uno stream NDJSON per aggiornamenti di progresso progressivi e il risultato finale.
    """
    check_and_log_ai_usage(auth_data["id_sede"], "import_excel_vendite")
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
