from fastapi import APIRouter, Depends, HTTPException
from database.config import Database
from utils.auth_utils import get_user_sede
from datetime import datetime, timedelta
import calendar

router = APIRouter(prefix="/api/statistiche", tags=["Statistiche e P&L"])
supabase = Database.get_client()

@router.get("/overview")
async def get_overview(periodo: str = "this_month", custom_start: str = None, custom_end: str = None, auth_data = Depends(get_user_sede)):
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

        # 1. Recupera Vendite
        vendite_res = supabase.table("vendite").select(
            "*, ricette(costo_ricetta_reale, prezzo_vendita_netto), anagrafica_rivendita(prezzo_vendita_netto, prezzo_acquisto_netto)"
        ).eq("id_sede", id_sede).gte("data_vendita", data_inizio_globali).lt("data_vendita", data_fine_inclusiva).execute()
        vendite_data = vendite_res.data or []

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

        # 4. Processamento vendite
        totale_ricavi = 0.0
        totale_food_cost = 0.0
        numero_ordini = 0

        for v in vendite_data:
            qta = v.get("quantita", 0)
            data_v_raw = v.get("data_vendita")
            if not data_v_raw: continue
            
            data_v = data_v_raw[:10]  # Prende solo YYYY-MM-DD ignorando eventuale tempo
            chiave = data_v if is_daily else data_v[:7]
            
            ricavo_unitario = 0
            costo_unitario = 0

            if v.get("ricette"):
                ricavo_unitario = v["ricette"].get("prezzo_vendita_netto", 0)
                costo_unitario = v["ricette"].get("costo_ricetta_reale", 0)
            elif v.get("anagrafica_rivendita"):
                ricavo_unitario = v["anagrafica_rivendita"].get("prezzo_vendita_netto", 0)
                costo_unitario = v["anagrafica_rivendita"].get("prezzo_acquisto_netto", 0)

            ricavo_tot = (ricavo_unitario * qta)
            costo_tot = (costo_unitario * qta)
            
            if chiave in andamento_dict:
                andamento_dict[chiave]["ricavi"] += ricavo_tot
                andamento_dict[chiave]["food_cost"] += costo_tot

            if data_inizio_globali <= data_v <= data_fine:
                totale_ricavi += ricavo_tot
                totale_food_cost += costo_tot
                numero_ordini += 1

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
                "totale_ricavi": round(totale_ricavi, 2),
                "totale_costi": round(totale_costi, 2),
                "margine_operativo": round(margine_operativo, 2),
                "margine_perc": round(margine_perc, 2),
                "numero_ordini": numero_ordini,
                "scontrino_medio": round(scontrino_medio, 2)
            },
            "andamento": andamento_list,
            "distribuzione": distribuzione
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/controllo-gestione/{anno}")
async def get_pl_annuale(anno: int, auth_data = Depends(get_user_sede)):
    try:
        id_sede = auth_data["id_sede"]

        # 1. Recupera TUTTI i Costi Generali (Fissi + Extra) dell'anno scelto
        costi_res = supabase.table("costi_anno_mese").select("*").eq("id_sede", id_sede).eq("anno", anno).execute()
        costi_data = costi_res.data or []

        # 2. Recupera TUTTE le Vendite dell'anno con un JOIN pazzesco per prendere il costo e il prezzo di quel prodotto
        vendite_res = supabase.table("vendite").select(
            "*, ricette(costo_ricetta_reale, prezzo_vendita_netto), anagrafica_rivendita(prezzo_vendita_netto, prezzo_acquisto_netto)"
        ).eq("id_sede", id_sede).gte("data_vendita", f"{anno}-01-01").lt("data_vendita", f"{anno+1}-01-01").execute()
        vendite_data = vendite_res.data or []

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

        # 5. Aggreghiamo le Vendite calcolando Ricavo e Food Cost reali
        for v in vendite_data:
            data_v_raw = v.get("data_vendita")
            if not data_v_raw: continue
            
            data_v = data_v_raw[:10]
            am = data_v[:7] # Estrae "YYYY-MM" da "YYYY-MM-DD"
            qta = v.get("quantita", 0)
            
            ricavo_unitario = 0
            costo_unitario = 0

            # Se è un prodotto del Menu (pizza, panino, ecc.)
            if v.get("ricette"):
                ricavo_unitario = v["ricette"].get("prezzo_vendita_netto", 0)
                costo_unitario = v["ricette"].get("costo_ricetta_reale", 0)
            # Se è un prodotto commerciale (coca cola, patatine, ecc.)
            elif v.get("anagrafica_rivendita"):
                ricavo_unitario = v["anagrafica_rivendita"].get("prezzo_vendita_netto", 0)
                costo_unitario = v["anagrafica_rivendita"].get("prezzo_acquisto_netto", 0)

            if am in report:
                report[am]["Ricavi"] += (ricavo_unitario * qta)
                report[am]["FoodCost"] += (costo_unitario * qta)

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
async def get_food_cost_analytics(
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

        # --- 1. Recupera vendite con JOIN completo ---
        vendite_res = supabase.table("vendite").select(
            "quantita, data_vendita, "
            "ricette(id, nome_ricetta, prezzo_vendita_netto, costo_ricetta_reale, id_categoria_prodotto, ingredienti_ricetta(quantita_per_kg, perc_scarto, anagrafica_materia_prima(articolo, prezzo_acquisto_netto))), "
            "anagrafica_rivendita(id, nome_articolo, prezzo_vendita_netto, prezzo_acquisto_netto, id_categoria_prodotto)"
        ).eq("id_sede", id_sede).gte("data_vendita", data_inizio_str).lt("data_vendita", data_fine_inclusiva).execute()
        vendite_data = vendite_res.data or []

        # --- 2. Recupera categorie per i nomi ---
        cat_res = supabase.table("categoria_prodotti").select("id, nome_categoria").eq("id_sede", id_sede).execute()
        categorie_map = {c["id"]: c["nome_categoria"] for c in (cat_res.data or [])}

        # --- 3. Variabili di aggregazione ---
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

        # --- 4. Elaborazione vendite ---
        for v in vendite_data:
            qta = v.get("quantita", 0)
            data_v = (v.get("data_vendita") or "")[:10]
            if not data_v:
                continue

            ricavo_u = 0.0
            fc_u = 0.0
            nome_prodotto = ""
            id_cat = None
            ingredienti_ricetta = []

            ricetta = v.get("ricette")
            riv = v.get("anagrafica_rivendita")

            if ricetta:
                ricavo_u = ricetta.get("prezzo_vendita_netto", 0) or 0
                fc_u = ricetta.get("costo_ricetta_reale", 0) or 0
                nome_prodotto = ricetta.get("nome_ricetta", "N/D")
                id_cat = ricetta.get("id_categoria_prodotto")
                ingredienti_ricetta = ricetta.get("ingredienti_ricetta") or []
            elif riv:
                ricavo_u = riv.get("prezzo_vendita_netto", 0) or 0
                fc_u = riv.get("prezzo_acquisto_netto", 0) or 0
                nome_prodotto = riv.get("nome_articolo", "N/D")
                id_cat = riv.get("id_categoria_prodotto")

            ricavo_tot = ricavo_u * qta
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
                mp = ing.get("anagrafica_materia_prima") or {}
                nome_ing = mp.get("articolo", "N/D")
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
async def get_ricette_breakdown(auth_data=Depends(get_user_sede)):
    """
    Restituisce per ogni prodotto finito il dettaglio completo degli ingredienti:
    - costo per ingrediente (con calcolo scarto)
    - % di ogni ingrediente sul food cost totale della ricetta
    - % di ogni ingrediente sul prezzo di vendita
    - margine per porzione
    """
    try:
        id_sede = auth_data["id_sede"]

        # Recupera tutte le ricette con ingredienti in cascata
        pf_res = supabase.table("ricette").select(
            "id, nome_ricetta, prezzo_vendita_netto, costo_ricetta_reale, "
            "ingredienti_ricetta(quantita_per_kg, perc_scarto, "
            "anagrafica_materia_prima(articolo, prezzo_acquisto_netto, unita_misura))"
        ).eq("id_sede", id_sede).execute()

        result = []

        for ricetta in (pf_res.data or []):
            prezzo = ricetta.get("prezzo_vendita_netto", 0) or 0
            costo_ricetta = ricetta.get("costo_ricetta_reale", 0) or 0
            margine = round(prezzo - costo_ricetta, 4)
            fc_perc = round((costo_ricetta / prezzo * 100) if prezzo > 0 else 0.0, 2)
            margine_perc = round(100 - fc_perc, 2)

            ingredienti = []
            for ing in (ricetta.get("ingredienti_ricetta") or []):
                mp = ing.get("anagrafica_materia_prima") or {}
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
                    "nome": mp.get("articolo", "N/D"),
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