from fastapi import APIRouter, Depends, HTTPException
from database.config import Database
from utils.auth_utils import get_user_sede
from datetime import datetime, timedelta
import calendar

router = APIRouter(prefix="/api/statistiche", tags=["Statistiche e P&L"])
supabase = Database.get_client()


def _andamento_vendite_python(id_sede: str, data_inizio: str, data_fine_esclusiva: str, group_by: str) -> list:
    """
    Sostituisce la (ex) funzione SQL stat_andamento_vendite. Calcola l'andamento
    delle vendite (ricavi, ricavi_lordo, food_cost, numero_vendite) raggruppato
    per giorno o mese ("day"/"month"), con le stesse chiavi di output che aveva
    la RPC, così da poter essere usata come sostituto diretto.

    Differenze volute rispetto alla vecchia RPC:
    - I ricavi usano il prezzo REALMENTE applicato alla vendita (vendite.prezzo_totale
      o prezzo_singolo*quantita), non più il prezzo attuale di listino: un cambio
      prezzo futuro non altera più le vendite passate. Il listino resta solo un
      fallback per righe non ancora "backfillate" (vedi sql/001_backfill_prezzo_vendite.sql).
    - Include anche vendite_sospese: sono vendite reali, solo non ancora abbinate
      a un prodotto specifico, quindi contano nei ricavi totali dell'attività
      (ma non nel food cost, che richiede un prodotto per essere calcolato).
    - Il food cost resta invece calcolato sui costi ATTUALI di ricette/articoli:
      non esiste (ancora) uno storico dei costi ingredienti al momento della vendita.
    """
    page_size = 1000

    def _fetch_all(table_name: str, select_cols: str):
        rows = []
        page = 0
        while True:
            res = supabase.table(table_name).select(select_cols).eq("id_sede", id_sede)\
                .gte("data_vendita", data_inizio).lt("data_vendita", data_fine_esclusiva)\
                .range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            rows.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1
        return rows

    vendite_data = _fetch_all("vendite", "quantita, data_vendita, id_ricetta, id_prodotto_commerciale, prezzo_singolo, prezzo_totale")
    sospese_data = _fetch_all("vendite_sospese", "quantita, data_vendita, prezzo_singolo, prezzo_totale")

    ids_ricette = {v["id_ricetta"] for v in vendite_data if v.get("id_ricetta")}
    ids_commerciali = {v["id_prodotto_commerciale"] for v in vendite_data if v.get("id_prodotto_commerciale")}

    ricette_map = {}
    if ids_ricette:
        res = supabase.table("ricette").select("id, prezzo_vendita_netto, prezzo_vendita_lordo, costo_ricetta_reale").in_("id", list(ids_ricette)).execute()
        ricette_map = {r["id"]: r for r in (res.data or [])}

    articoli_map = {}
    if ids_commerciali:
        res = supabase.table("articoli").select("id, prezzo_vendita_netto, prezzo_vendita_lordo, prezzo_acquisto_netto").in_("id", list(ids_commerciali)).execute()
        articoli_map = {a["id"]: a for a in (res.data or [])}

    def _chiave_periodo(data_iso: str) -> str:
        return data_iso[:10] if group_by == "day" else data_iso[:7]

    aggregato = {}

    def _riga(chiave: str):
        if chiave not in aggregato:
            aggregato[chiave] = {"periodo": chiave, "ricavi": 0.0, "ricavi_lordo": 0.0, "food_cost": 0.0, "numero_vendite": 0}
        return aggregato[chiave]

    for v in vendite_data:
        data_v = (v.get("data_vendita") or "")[:10]
        if not data_v:
            continue
        qta = v.get("quantita") or 0

        anagrafica = None
        food_cost_u = 0.0
        if v.get("id_ricetta") is not None:
            anagrafica = ricette_map.get(v["id_ricetta"])
            if anagrafica:
                food_cost_u = anagrafica.get("costo_ricetta_reale") or 0.0
        elif v.get("id_prodotto_commerciale") is not None:
            anagrafica = articoli_map.get(v["id_prodotto_commerciale"])
            if anagrafica:
                food_cost_u = anagrafica.get("prezzo_acquisto_netto") or 0.0

        listino_netto = (anagrafica.get("prezzo_vendita_netto") or 0.0) if anagrafica else 0.0
        listino_lordo = (anagrafica.get("prezzo_vendita_lordo") or 0.0) if anagrafica else 0.0

        # Ricavo netto realmente applicato: prima il totale salvato sulla vendita,
        # poi l'unitario salvato, infine il listino attuale come ultima spiaggia
        # (solo per vendite registrate prima del backfill).
        if v.get("prezzo_totale") is not None:
            ricavo_netto = v["prezzo_totale"]
        elif v.get("prezzo_singolo") is not None:
            ricavo_netto = v["prezzo_singolo"] * qta
        else:
            ricavo_netto = listino_netto * qta

        # Il lordo non è mai stato salvato sulla vendita: lo stimiamo applicando
        # al ricavo netto reale il rapporto lordo/netto ATTUALE del prodotto
        # (l'aliquota IVA cambia raramente, è un'approssimazione ragionevole).
        rapporto_lordo = (listino_lordo / listino_netto) if listino_netto > 0 else 1.0
        ricavo_lordo = ricavo_netto * rapporto_lordo

        riga = _riga(_chiave_periodo(data_v))
        riga["ricavi"] += ricavo_netto
        riga["ricavi_lordo"] += ricavo_lordo
        riga["food_cost"] += food_cost_u * qta
        riga["numero_vendite"] += 1

    for s in sospese_data:
        data_v = (s.get("data_vendita") or "")[:10]
        if not data_v:
            continue
        qta = s.get("quantita") or 0
        if s.get("prezzo_totale") is not None:
            ricavo_netto = s["prezzo_totale"]
        elif s.get("prezzo_singolo") is not None:
            ricavo_netto = s["prezzo_singolo"] * qta
        else:
            ricavo_netto = 0.0  # nessun prodotto associato e nessun prezzo rilevato: non calcolabile

        riga = _riga(_chiave_periodo(data_v))
        # Senza prodotto non c'è modo di stimare un lordo diverso dal netto.
        riga["ricavi"] += ricavo_netto
        riga["ricavi_lordo"] += ricavo_netto
        riga["numero_vendite"] += 1

    return list(aggregato.values())


@router.get("/overview")
def get_overview(periodo: str = "this_month", custom_start: str = None, custom_end: str = None, auth_data = Depends(get_user_sede)):
    try:
        id_sede = auth_data["id_sede"]

        oggi_date = datetime.now().date()
        is_daily = True
        
        if periodo == "custom" and custom_start and custom_end:
            data_inizio_date = datetime.strptime(custom_start, "%Y-%m-%d").date()
            data_fine_date = datetime.strptime(custom_end, "%Y-%m-%d").date()
            
            # Se l'intervallo è > 60 giorni, raggruppiamo per mese per non affollare il grafico
            delta_days = (data_fine_date - data_inizio_date).days
            if delta_days > 60:
                is_daily = False
        elif periodo == "oggi":
            data_inizio_date = oggi_date
            data_fine_date = oggi_date
        elif periodo == "last_7_days":
            data_inizio_date = oggi_date - timedelta(days=6)
            data_fine_date = oggi_date
        elif periodo == "this_month":
            data_inizio_date = oggi_date.replace(day=1)
            data_fine_date = oggi_date
        else:
            data_inizio_date = oggi_date - timedelta(days=29)
            data_fine_date = oggi_date
            
        data_inizio_globali = data_inizio_date.strftime("%Y-%m-%d")
        data_fine = data_fine_date.strftime("%Y-%m-%d")
        data_fine_inclusiva = (data_fine_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # 1. Andamento delle vendite (ricavi/food cost), calcolato dal prezzo
        # REALMENTE applicato su vendite/vendite_sospese — non più dal listino attuale.
        andamento_vendite = _andamento_vendite_python(
            id_sede, data_inizio_globali, data_fine_inclusiva, "day" if is_daily else "month"
        )

        # 2. Recupera Costi Fissi
        costi_res = supabase.table("costi_anno_mese").select("*").eq("id_sede", id_sede).execute()
        costi_data = costi_res.data or []
        
        costi_fissi_mensili = {}
        for c in costi_data:
            am = c.get("anno_mese")
            if am:
                costi_fissi_mensili[am] = costi_fissi_mensili.get(am, 0) + c.get("importo", 0)

        # 3. Creazione andamento temporale (giornaliero o mensile)
        andamento_dict = {}
        
        if is_daily:
            delta = data_fine_date - data_inizio_date
            for i in range(delta.days + 1):
                d = data_inizio_date + timedelta(days=i)
                chiave = d.strftime("%Y-%m-%d")
                mese_chiave = d.strftime("%Y-%m")
                
                importo_mensile = costi_fissi_mensili.get(mese_chiave, 0)
                _, giorni_nel_mese = calendar.monthrange(d.year, d.month)
                importo_giornaliero = importo_mensile / giorni_nel_mese if giorni_nel_mese else 0
                
                andamento_dict[chiave] = {"data": chiave, "ricavi": 0.0, "food_cost": 0.0, "costi_fissi": importo_giornaliero}
        else:
            # Creiamo un array di mesi tra data_inizio e data_fine
            start_month = data_inizio_date.month
            start_year = data_inizio_date.year
            end_month = data_fine_date.month
            end_year = data_fine_date.year
            
            curr_y = start_year
            curr_m = start_month
            
            while curr_y < end_year or (curr_y == end_year and curr_m <= end_month):
                chiave = f"{curr_y}-{curr_m:02d}"
                importo_mensile = costi_fissi_mensili.get(chiave, 0)
                andamento_dict[chiave] = {"data": chiave, "ricavi": 0.0, "food_cost": 0.0, "costi_fissi": importo_mensile}
                curr_m += 1
                if curr_m > 12:
                    curr_m = 1
                    curr_y += 1

        # 4. Applica l'andamento aggregato (già filtrato per id_sede/periodo dalla funzione SQL,
        # quindi ogni riga restituita rientra nell'intervallo richiesto)
        totale_ricavi = 0.0
        totale_ricavi_lordo = 0.0
        totale_food_cost = 0.0
        numero_ordini = 0

        for row in andamento_vendite:
            chiave = row["periodo"]
            ricavi_riga = row.get("ricavi") or 0.0
            ricavi_lordo_riga = row.get("ricavi_lordo") or 0.0
            food_cost_riga = row.get("food_cost") or 0.0
            ordini_riga = row.get("numero_vendite") or 0

            if chiave in andamento_dict:
                andamento_dict[chiave]["ricavi"] += ricavi_riga
                andamento_dict[chiave]["food_cost"] += food_cost_riga

            totale_ricavi += ricavi_riga
            totale_ricavi_lordo += ricavi_lordo_riga
            totale_food_cost += food_cost_riga
            numero_ordini += ordini_riga

        # 5. Metriche Globali
        totale_costi_generali = sum(item["costi_fissi"] for item in andamento_dict.values() if item["data"] >= data_inizio_globali and item["data"] <= data_fine)

        # Nel caso mensile, la chiave è YYYY-MM, verifichiamo la compatibilità
        if not is_daily:
            mese_inizio = data_inizio_globali[:7]
            mese_fine = data_fine[:7]
            totale_costi_generali = sum(item["costi_fissi"] for item in andamento_dict.values() if item["data"] >= mese_inizio and item["data"] <= mese_fine)

        totale_costi = totale_food_cost + totale_costi_generali
        margine_operativo = totale_ricavi - totale_costi
        margine_perc = (margine_operativo / totale_ricavi * 100) if totale_ricavi > 0 else 0.0
        scontrino_medio = (totale_ricavi / numero_ordini) if numero_ordini > 0 else 0.0

        # Conto economico del periodo (Vendite Nette -> Margine -> Risultato):
        # margine_operativo/margine_perc sopra corrispondono già a "Risultato del
        # Periodo"/"Risultato del periodo %" — qui aggiungiamo solo i valori intermedi
        # (vendite lorde, food cost, costi generali) e il margine lordo, per la nuova
        # sezione "Conto Economico" della pagina Statistiche.
        margine_lordo = totale_ricavi - totale_food_cost
        margine_perc_lordo = (margine_lordo / totale_ricavi * 100) if totale_ricavi > 0 else 0.0

        andamento_list = []
        for chiave in sorted(andamento_dict.keys()):
            v = andamento_dict[chiave]
            # Omettiamo i mesi futuri vuoti nel grafico annuale
            if not is_daily and chiave > data_fine[:7] and v["ricavi"] == 0 and v["costi_fissi"] == 0:
                continue
                
            ricavi = v["ricavi"]
            costi_tot = v["food_cost"] + v["costi_fissi"]
            margine = ricavi - costi_tot
            andamento_list.append({
                "data": v["data"],
                "ricavi": round(ricavi, 2),
                "food_cost": round(v["food_cost"], 2),
                "costi_fissi": round(v["costi_fissi"], 2),
                "margine": round(margine, 2)
            })
            
        # Il Pie Chart non gestisce valori negativi (li disegna in assoluto sballando tutto).
        # Quindi se c'è un utile, la torta rappresenta i RICAVI (Food Cost + Costi Fissi + Margine).
        # Se c'è una perdita, la torta rappresenta solo i COSTI (Food Cost + Costi Fissi).
        distribuzione = [
            {"label": "Food Cost", "valore": round(totale_food_cost, 2), "colore": "#f87171"},
            {"label": "Costi Fissi", "valore": round(totale_costi_generali, 2), "colore": "#fbbf24"}
        ]
        
        if margine_operativo > 0:
            distribuzione.append({"label": "Margine Netto", "valore": round(margine_operativo, 2), "colore": "#34d399"})

        return {
            "globali": {
                # Campi storici (usati anche da Dashboard) — invariati
                "totale_ricavi": round(totale_ricavi, 2),
                "totale_costi": round(totale_costi, 2),
                "margine_operativo": round(margine_operativo, 2),
                "margine_perc": round(margine_perc, 2),
                "numero_ordini": numero_ordini,
                "scontrino_medio": round(scontrino_medio, 2),

                # Conto Economico del periodo (sezione Statistiche)
                "vendite_netto_iva": round(totale_ricavi, 2),
                "vendite_con_iva": round(totale_ricavi_lordo, 2),
                "food_cost": round(totale_food_cost, 2),
                "margine": round(margine_lordo, 2),
                "margine_perc_lordo": round(margine_perc_lordo, 2),
                "costi_generali": round(totale_costi_generali, 2),
                "risultato_periodo": round(margine_operativo, 2),
                "risultato_periodo_perc": round(margine_perc, 2)
            },
            "andamento": andamento_list,
            "distribuzione": distribuzione
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/controllo-gestione/{anno}")
def get_pl_annuale(anno: int, auth_data = Depends(get_user_sede)):
    try:
        id_sede = auth_data["id_sede"]

        # 1. Recupera TUTTI i Costi Generali (Fissi + Extra) dell'anno scelto
        costi_res = supabase.table("costi_anno_mese").select("*").eq("id_sede", id_sede).eq("anno", anno).execute()
        costi_data = costi_res.data or []

        # 2. Andamento mensile delle vendite dell'anno, dal prezzo REALMENTE
        # applicato su vendite/vendite_sospese — non più dal listino attuale.
        andamento_vendite = _andamento_vendite_python(
            id_sede, f"{anno}-01-01", f"{anno + 1}-01-01", "month"
        )

        # 3. Prepariamo il contenitore vuoto per i 12 mesi
        report = {}
        for mese in range(1, 13):
            chiave_mese = f"{anno}-{mese:02d}" # Formato "2026-01", "2026-02", ecc.
            report[chiave_mese] = {
                "KiaveAnnoMese": chiave_mese,
                "Ricavi": 0.0,
                "FoodCost": 0.0,
                "CostiGenerali": 0.0
            }

        # 4. Aggreghiamo i Costi Generali
        for costo in costi_data:
            am = costo.get("anno_mese") # Deve corrispondere a "2026-01"
            if am in report:
                report[am]["CostiGenerali"] += costo.get("importo", 0)

        # 5. Applichiamo l'andamento mensile già aggregato dalla funzione SQL
        for row in andamento_vendite:
            am = row["periodo"]  # "YYYY-MM"
            if am in report:
                report[am]["Ricavi"] += row.get("ricavi") or 0.0
                report[am]["FoodCost"] += row.get("food_cost") or 0.0

        # 6. Calcolo Matematico Finale (Margini e Utile)
        risultato_finale = []
        for am in sorted(report.keys()):
            r = report[am]
            margine = r["Ricavi"] - r["FoodCost"]
            margine_perc = (margine / r["Ricavi"] * 100) if r["Ricavi"] > 0 else 0.0
            utile = margine - r["CostiGenerali"]

            # Arrotondiamo tutto a 2 decimali per il frontend
            r["Margine"] = round(margine, 2)
            r["MarginePerc"] = round(margine_perc, 2)
            r["Ricavi"] = round(r["Ricavi"], 2)
            r["FoodCost"] = round(r["FoodCost"], 2)
            r["CostiGenerali"] = round(r["CostiGenerali"], 2)
            r["RisultatoDiPeriodo"] = round(utile, 2)
            
            risultato_finale.append(r)

        return risultato_finale
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# FOOD COST ANALYTICS
# ==========================================
@router.get("/food-cost")
def get_food_cost_analytics(
    periodo: str = "last_30_days",
    custom_start: str = None,
    custom_end: str = None,
    auth_data=Depends(get_user_sede)
    ):
    """
    Endpoint di analisi approfondita del Food Cost.
    Restituisce:
    - KPI globali (FC%, soglia ideale, prodotti a rischio)
    - Top prodotti per food cost % (sia del menu che rivendita)
    - Breakdown ingredienti più costosi
    - Trend giornaliero del food cost %
    - Distribuzione FC per categoria
    """
    try:
        id_sede = auth_data["id_sede"]
        oggi_date = datetime.now().date()

        # --- Date Range ---
        if periodo == "custom" and custom_start and custom_end:
            data_inizio_date = datetime.strptime(custom_start, "%Y-%m-%d").date()
            data_fine_date = datetime.strptime(custom_end, "%Y-%m-%d").date()
        elif periodo == "oggi":
            data_inizio_date = oggi_date
            data_fine_date = oggi_date
        elif periodo == "last_7_days":
            data_inizio_date = oggi_date - timedelta(days=6)
            data_fine_date = oggi_date
        elif periodo == "last_30_days":
            data_inizio_date = oggi_date - timedelta(days=29)
            data_fine_date = oggi_date
        elif periodo == "this_month":
            data_inizio_date = oggi_date.replace(day=1)
            data_fine_date = oggi_date
        else:
            data_inizio_date = oggi_date - timedelta(days=29)
            data_fine_date = oggi_date

        data_inizio_str = data_inizio_date.strftime("%Y-%m-%d")
        data_fine_str = data_fine_date.strftime("%Y-%m-%d")
        data_fine_inclusiva = (data_fine_date + timedelta(days=1)).strftime("%Y-%m-%d")
        delta_days = (data_fine_date - data_inizio_date).days

        # --- 1. Recupera le vendite del periodo (solo i campi essenziali, paginato:
        # una vendita può facilmente superare le 1000 righe di default di Supabase). ---
        vendite_data = []
        page = 0
        page_size = 1000
        while True:
            res = supabase.table("vendite").select(
                "quantita, data_vendita, id_ricetta, id_prodotto_commerciale, prezzo_singolo, prezzo_totale"
            ).eq("id_sede", id_sede)\
             .gte("data_vendita", data_inizio_str)\
             .lt("data_vendita", data_fine_inclusiva)\
             .range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            vendite_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1

        # --- 2. Recupera UNA SOLA VOLTA i dati "anagrafici" di ricette e articoli
        # (prezzo, costo, ingredienti), invece di rifare il join per ogni singola
        # vendita: lo stesso prodotto venduto 500 volte in un mese scaricherebbe
        # altrimenti 500 volte l'intero albero ingredienti_ricetta->articoli. ---
        ricette_data = []
        page = 0
        while True:
            res = supabase.table("ricette").select(
                "id, nome_ricetta, prezzo_vendita_netto, costo_ricetta_reale, id_categoria_prodotto, "
                "ingredienti_ricetta(quantita_per_kg, perc_scarto, articoli(nome_articolo, prezzo_acquisto_netto))"
            ).eq("id_sede", id_sede).range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            ricette_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1
        ricette_map = {r["id"]: r for r in ricette_data}

        articoli_data = []
        page = 0
        while True:
            res = supabase.table("articoli").select(
                "id, nome_articolo, prezzo_vendita_netto, prezzo_acquisto_netto, id_categoria_prodotto"
            ).eq("id_sede", id_sede).range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            articoli_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1
        articoli_map = {a["id"]: a for a in articoli_data}

        # --- 3. Recupera categorie per i nomi ---
        cat_res = supabase.table("categoria_prodotti").select("id, nome_categoria").eq("id_sede", id_sede).execute()
        categorie_map = {c["id"]: c["nome_categoria"] for c in (cat_res.data or [])}

        # --- 4. Variabili di aggregazione ---
        totale_ricavi = 0.0
        totale_food_cost = 0.0

        # Per prodotto: { nome: { ricavi, fc, qta } }
        prodotti_dict = {}

        # Ingredienti più costosi (incidenza sulla spesa totale materie prime)
        ingredienti_dict = {}

        # Trend giornaliero { data: { ricavi, fc } }
        trend_dict = {}
        for i in range(delta_days + 1):
            d = data_inizio_date + timedelta(days=i)
            trend_dict[d.strftime("%Y-%m-%d")] = {"data": d.strftime("%Y-%m-%d"), "ricavi": 0.0, "food_cost": 0.0}

        # Distribuzione per categoria { id_cat: { nome, ricavi, fc } }
        categorie_dict = {}

        # --- 5. Elaborazione vendite ---
        for v in vendite_data:
            qta = v.get("quantita", 0)
            data_v = (v.get("data_vendita") or "")[:10]
            if not data_v:
                continue

            fc_u = 0.0
            listino_netto = 0.0
            nome_prodotto = ""
            id_cat = None
            ingredienti_ricetta = []

            id_ricetta = v.get("id_ricetta")
            id_prodotto = v.get("id_prodotto_commerciale")
            ricetta = ricette_map.get(id_ricetta) if id_ricetta is not None else None
            riv = articoli_map.get(id_prodotto) if id_prodotto is not None else None

            if ricetta:
                listino_netto = ricetta.get("prezzo_vendita_netto", 0) or 0
                fc_u = ricetta.get("costo_ricetta_reale", 0) or 0
                nome_prodotto = ricetta.get("nome_ricetta", "N/D")
                id_cat = ricetta.get("id_categoria_prodotto")
                ingredienti_ricetta = ricetta.get("ingredienti_ricetta") or []
            elif riv:
                listino_netto = riv.get("prezzo_vendita_netto", 0) or 0
                fc_u = riv.get("prezzo_acquisto_netto", 0) or 0
                nome_prodotto = riv.get("nome_articolo", "N/D")
                id_cat = riv.get("id_categoria_prodotto")

            # Ricavo REALMENTE applicato alla vendita: prima il totale salvato,
            # poi l'unitario salvato, infine il listino attuale come ultima
            # spiaggia (solo per vendite non ancora "backfillate").
            if v.get("prezzo_totale") is not None:
                ricavo_tot = v["prezzo_totale"]
            elif v.get("prezzo_singolo") is not None:
                ricavo_tot = v["prezzo_singolo"] * qta
            else:
                ricavo_tot = listino_netto * qta
            fc_tot = fc_u * qta

            totale_ricavi += ricavo_tot
            totale_food_cost += fc_tot

            # Aggregazione per prodotto
            if nome_prodotto not in prodotti_dict:
                prodotti_dict[nome_prodotto] = {"nome": nome_prodotto, "ricavi": 0.0, "food_cost": 0.0, "qta_venduta": 0, "id_cat": id_cat}
            prodotti_dict[nome_prodotto]["ricavi"] += ricavo_tot
            prodotti_dict[nome_prodotto]["food_cost"] += fc_tot
            prodotti_dict[nome_prodotto]["qta_venduta"] += qta

            # Aggregazione per categoria
            if id_cat is not None:
                if id_cat not in categorie_dict:
                    categorie_dict[id_cat] = {"nome_categoria": categorie_map.get(id_cat, f"Cat. {id_cat}"), "ricavi": 0.0, "food_cost": 0.0}
                categorie_dict[id_cat]["ricavi"] += ricavo_tot
                categorie_dict[id_cat]["food_cost"] += fc_tot

            # Trend giornaliero
            if data_v in trend_dict:
                trend_dict[data_v]["ricavi"] += ricavo_tot
                trend_dict[data_v]["food_cost"] += fc_tot

            # Ingredienti (solo per prodotti del menu con ricette)
            for ing in ingredienti_ricetta:
                mp = ing.get("articoli") or {}
                nome_ing = mp.get("nome_articolo", "N/D")
                costo_netto_mp = mp.get("prezzo_acquisto_netto", 0) or 0
                qta_per_kg = ing.get("quantita_per_kg", 0) or 0
                perc_scarto = ing.get("perc_scarto", 0) or 0
                resa = 1 - (perc_scarto / 100)
                qta_eff = (qta_per_kg / resa) if resa > 0 else qta_per_kg
                costo_ing_per_piatto = qta_eff * costo_netto_mp
                costo_ing_totale = costo_ing_per_piatto * qta  # moltiplicato per le porzioni vendute

                if nome_ing not in ingredienti_dict:
                    ingredienti_dict[nome_ing] = {"nome": nome_ing, "costo_totale": 0.0, "costo_netto_unitario": costo_netto_mp}
                ingredienti_dict[nome_ing]["costo_totale"] += costo_ing_totale

        # --- 5. KPI Globali ---
        fc_perc_globale = (totale_food_cost / totale_ricavi * 100) if totale_ricavi > 0 else 0.0
        soglia_ideale = 30.0  # standard ristorazione
        delta_dalla_soglia = fc_perc_globale - soglia_ideale

        # --- 6. Top prodotti per FC% (ordinati dal peggiore al migliore) ---
        top_prodotti = []
        for nome, p in prodotti_dict.items():
            fc_perc = (p["food_cost"] / p["ricavi"] * 100) if p["ricavi"] > 0 else 0.0
            nome_cat = categorie_map.get(p["id_cat"], "N/D") if p["id_cat"] else "N/D"
            top_prodotti.append({
                "nome": nome,
                "categoria": nome_cat,
                "ricavi": round(p["ricavi"], 2),
                "food_cost": round(p["food_cost"], 2),
                "fc_perc": round(fc_perc, 2),
                "qta_venduta": p["qta_venduta"],
                "alert": fc_perc > soglia_ideale  # True se sopra soglia
            })
        top_prodotti.sort(key=lambda x: x["fc_perc"], reverse=True)

        # --- 7. Top ingredienti per costo totale (top 8) ---
        ingredienti_list = sorted(
            [{"nome": k, "costo_totale": round(v["costo_totale"], 2), "costo_unitario": round(v["costo_netto_unitario"], 4)} for k, v in ingredienti_dict.items()],
            key=lambda x: x["costo_totale"],
            reverse=True
        )[:8]

        # Aggiungo la % sul totale food cost
        for ing in ingredienti_list:
            ing["perc_sul_fc"] = round((ing["costo_totale"] / totale_food_cost * 100) if totale_food_cost > 0 else 0.0, 2)

        # --- 8. Trend giornaliero (con FC%) ---
        trend_list = []
        for chiave in sorted(trend_dict.keys()):
            t = trend_dict[chiave]
            fc_perc_giorno = (t["food_cost"] / t["ricavi"] * 100) if t["ricavi"] > 0 else 0.0
            trend_list.append({
                "data": t["data"],
                "ricavi": round(t["ricavi"], 2),
                "food_cost": round(t["food_cost"], 2),
                "fc_perc": round(fc_perc_giorno, 2),
                "soglia": soglia_ideale
            })

        # --- 9. Distribuzione per categoria ---
        categorie_list = []
        for id_cat, c in categorie_dict.items():
            fc_p = (c["food_cost"] / c["ricavi"] * 100) if c["ricavi"] > 0 else 0.0
            categorie_list.append({
                "nome_categoria": c["nome_categoria"],
                "food_cost": round(c["food_cost"], 2),
                "ricavi": round(c["ricavi"], 2),
                "fc_perc": round(fc_p, 2)
            })
        categorie_list.sort(key=lambda x: x["food_cost"], reverse=True)

        # --- 10. Prodotti a rischio (FC% > 30%) ---
        prodotti_a_rischio = [p for p in top_prodotti if p["alert"]]

        return {
            "kpi": {
                "fc_perc_globale": round(fc_perc_globale, 2),
                "totale_food_cost": round(totale_food_cost, 2),
                "totale_ricavi": round(totale_ricavi, 2),
                "soglia_ideale": soglia_ideale,
                "delta_dalla_soglia": round(delta_dalla_soglia, 2),
                "prodotti_a_rischio_count": len(prodotti_a_rischio),
                "stato": "critico" if fc_perc_globale > 35 else ("attenzione" if fc_perc_globale > soglia_ideale else "ok")
            },
            "top_prodotti": top_prodotti[:10],
            "ingredienti_top_costo": ingredienti_list,
            "trend": trend_list,
            "per_categoria": categorie_list,
            "prodotti_a_rischio": prodotti_a_rischio[:5]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# RICETTE BREAKDOWN — Anatomia per prodotto
# ==========================================
@router.get("/ricette-breakdown")
def get_ricette_breakdown(auth_data=Depends(get_user_sede)):
    """
    Restituisce per ogni prodotto finito il dettaglio completo degli ingredienti:
    - costo per ingrediente (con calcolo scarto)
    - % di ogni ingrediente sul food cost totale della ricetta
    - % di ogni ingrediente sul prezzo di vendita
    - margine per porzione
    """
    try:
        id_sede = auth_data["id_sede"]

        # Recupera tutte le ricette con ingredienti in cascata (paginato per
        # sicurezza, stesso pattern usato altrove per evitare il limite di
        # default di Supabase sulle righe restituite)
        pf_data = []
        page = 0
        page_size = 500
        while True:
            res = supabase.table("ricette").select(
                "id, nome_ricetta, prezzo_vendita_netto, prezzo_vendita_lordo, costo_ricetta_reale, "
                "ingredienti_ricetta(quantita_per_kg, perc_scarto, "
                "articoli(nome_articolo, prezzo_acquisto_netto, unita_misura))"
            ).eq("id_sede", id_sede).range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            pf_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1

        result = []

        for ricetta in pf_data:
            prezzo = ricetta.get("prezzo_vendita_netto", 0) or 0
            prezzo_lordo = ricetta.get("prezzo_vendita_lordo", 0) or 0
            costo_ricetta = ricetta.get("costo_ricetta_reale", 0) or 0
            margine = round(prezzo - costo_ricetta, 4)
            fc_perc = round((costo_ricetta / prezzo * 100) if prezzo > 0 else 0.0, 2)
            margine_perc = round(100 - fc_perc, 2)

            ingredienti = []
            for ing in (ricetta.get("ingredienti_ricetta") or []):
                mp = ing.get("articoli") or {}
                costo_netto = mp.get("prezzo_acquisto_netto", 0) or 0
                unita = mp.get("unita_misura", "kg")
                qta_base = ing.get("quantita_per_kg", 0) or 0
                perc_scarto = ing.get("perc_scarto", 0) or 0
                resa = 1 - (perc_scarto / 100)
                qta_effettiva = (qta_base / resa) if resa > 0 else qta_base
                costo_ing = round(qta_effettiva * costo_netto, 5)

                perc_sulla_ricetta = round((costo_ing / costo_ricetta * 100) if costo_ricetta > 0 else 0.0, 2)
                perc_sul_prezzo = round((costo_ing / prezzo * 100) if prezzo > 0 else 0.0, 2)

                ingredienti.append({
                    "nome": mp.get("nome_articolo", "N/D"),
                    "unita_misura": unita,
                    "costo_per_unita": round(costo_netto, 4),
                    "quantita_base": round(qta_base, 4),
                    "perc_scarto": perc_scarto,
                    "quantita_effettiva": round(qta_effettiva, 4),
                    "costo_ingrediente": round(costo_ing, 4),
                    "perc_sulla_ricetta": perc_sulla_ricetta,
                    "perc_sul_prezzo": perc_sul_prezzo
                })

            # Ordina ingredienti dal più costoso al meno costoso
            ingredienti.sort(key=lambda x: x["costo_ingrediente"], reverse=True)

            result.append({
                "id": ricetta.get("id"),
                "nome": ricetta.get("nome_ricetta", "N/D"),
                "prezzo_vendita": round(prezzo, 2),
                "prezzo_vendita_lordo": round(prezzo_lordo, 2),
                "costo_totale_ricetta": round(costo_ricetta, 4),
                "margine": round(margine, 4),
                "fc_perc": fc_perc,
                "margine_perc": margine_perc,
                "num_ingredienti": len(ingredienti),
                "ingredienti": ingredienti
            })

        # Ordina per FC% più alto (i più critici prima)
        result.sort(key=lambda x: x["fc_perc"], reverse=True)
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ANDAMENTO PRODOTTI — Vendite/Food Cost/Ricarico per prodotto nel periodo
# ==========================================
@router.get("/andamento-prodotti")
def get_andamento_prodotti(
    periodo: str = "last_30_days",
    custom_start: str = None,
    custom_end: str = None,
    auth_data=Depends(get_user_sede)
    ):
    """
    Per ogni prodotto realmente venduto nel periodo (ricette del menu E articoli
    in rivendita diretta), restituisce quantità vendute, vendite nette, food cost
    totale e ricarico (vendite nette / food cost). Nota bene: ricette/articoli
    senza vendite nel periodo non compaiono (report basato sulle vendite reali,
    non sul catalogo completo). Le vendite nette usano il prezzo REALMENTE
    applicato alla vendita (vendite.prezzo_totale/prezzo_singolo), con il listino
    attuale come fallback solo per vendite non ancora "backfillate". Il food cost
    resta invece calcolato sul costo ATTUALE di ricette.costo_ricetta_reale /
    articoli.prezzo_acquisto_netto: non esiste (ancora) uno storico dei costi
    ingredienti al momento della vendita, quindi un periodo passato riflette il
    costo di oggi, non quello in vigore all'epoca.
    """
    try:
        id_sede = auth_data["id_sede"]
        oggi_date = datetime.now().date()

        # --- Date Range (stesso pattern di /food-cost) ---
        if periodo == "custom" and custom_start and custom_end:
            data_inizio_date = datetime.strptime(custom_start, "%Y-%m-%d").date()
            data_fine_date = datetime.strptime(custom_end, "%Y-%m-%d").date()
        elif periodo == "oggi":
            data_inizio_date = oggi_date
            data_fine_date = oggi_date
        elif periodo == "last_7_days":
            data_inizio_date = oggi_date - timedelta(days=6)
            data_fine_date = oggi_date
        elif periodo == "last_30_days":
            data_inizio_date = oggi_date - timedelta(days=29)
            data_fine_date = oggi_date
        elif periodo == "this_month":
            data_inizio_date = oggi_date.replace(day=1)
            data_fine_date = oggi_date
        else:
            data_inizio_date = oggi_date - timedelta(days=29)
            data_fine_date = oggi_date

        data_inizio_str = data_inizio_date.strftime("%Y-%m-%d")
        data_fine_inclusiva = (data_fine_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # --- 1. Vendite del periodo (paginato) ---
        vendite_data = []
        page = 0
        page_size = 1000
        while True:
            res = supabase.table("vendite").select(
                "quantita, data_vendita, id_ricetta, id_prodotto_commerciale, prezzo_singolo, prezzo_totale"
            ).eq("id_sede", id_sede)\
             .gte("data_vendita", data_inizio_str)\
             .lt("data_vendita", data_fine_inclusiva)\
             .range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            vendite_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1

        # --- 2. Anagrafica ricette e articoli, recuperata una sola volta ---
        ricette_data = []
        page = 0
        while True:
            res = supabase.table("ricette").select(
                "id, nome_ricetta, prezzo_vendita_netto, costo_ricetta_reale"
            ).eq("id_sede", id_sede).range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            ricette_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1
        ricette_map = {r["id"]: r for r in ricette_data}

        articoli_data = []
        page = 0
        while True:
            res = supabase.table("articoli").select(
                "id, nome_articolo, prezzo_vendita_netto, prezzo_acquisto_netto"
            ).eq("id_sede", id_sede).range(page * page_size, (page + 1) * page_size - 1).execute()
            if not res.data:
                break
            articoli_data.extend(res.data)
            if len(res.data) < page_size:
                break
            page += 1
        articoli_map = {a["id"]: a for a in articoli_data}

        # --- 3. Aggregazione per prodotto (chiave = tipo+id, non il nome: due
        # prodotti diversi potrebbero chiamarsi allo stesso modo) ---
        prodotti_dict = {}
        for v in vendite_data:
            qta = v.get("quantita", 0) or 0
            id_ricetta = v.get("id_ricetta")
            id_prodotto = v.get("id_prodotto_commerciale")
            ricetta = ricette_map.get(id_ricetta) if id_ricetta is not None else None
            articolo = articoli_map.get(id_prodotto) if id_prodotto is not None else None

            listino_netto = 0.0
            if ricetta:
                chiave = f"ricetta-{id_ricetta}"
                listino_netto = ricetta.get("prezzo_vendita_netto", 0) or 0
                fc_u = ricetta.get("costo_ricetta_reale", 0) or 0
                nome = ricetta.get("nome_ricetta", "N/D")
                tipo = "ricetta"
            elif articolo:
                chiave = f"articolo-{id_prodotto}"
                listino_netto = articolo.get("prezzo_vendita_netto", 0) or 0
                fc_u = articolo.get("prezzo_acquisto_netto", 0) or 0
                nome = articolo.get("nome_articolo", "N/D")
                tipo = "articolo"
            else:
                continue  # vendita sospesa/non risolta: non associabile a un prodotto

            # Ricavo REALMENTE applicato alla vendita: prima il totale salvato,
            # poi l'unitario salvato, infine il listino attuale come ultima
            # spiaggia (solo per vendite non ancora "backfillate").
            if v.get("prezzo_totale") is not None:
                ricavo_tot = v["prezzo_totale"]
            elif v.get("prezzo_singolo") is not None:
                ricavo_tot = v["prezzo_singolo"] * qta
            else:
                ricavo_tot = listino_netto * qta

            if chiave not in prodotti_dict:
                prodotti_dict[chiave] = {"id": chiave, "nome": nome, "tipo": tipo, "ricavi": 0.0, "food_cost": 0.0, "qta": 0.0}
            prodotti_dict[chiave]["ricavi"] += ricavo_tot
            prodotti_dict[chiave]["food_cost"] += fc_u * qta
            prodotti_dict[chiave]["qta"] += qta

        # --- 4. Output ---
        result = []
        for p in prodotti_dict.values():
            ricarico = (p["ricavi"] / p["food_cost"]) if p["food_cost"] > 0 else None
            result.append({
                "id": p["id"],
                "nome": p["nome"],
                "tipo": p["tipo"],
                "quantita_venduta": round(p["qta"], 2),
                "totale_vendite_nette": round(p["ricavi"], 2),
                "food_cost_totale": round(p["food_cost"], 2),
                "ricarico": round(ricarico, 2) if ricarico is not None else None
            })

        result.sort(key=lambda x: x["totale_vendite_nette"], reverse=True)
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))