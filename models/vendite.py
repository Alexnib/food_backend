from pydantic import BaseModel, model_validator
from typing import Optional, List
from datetime import date

class VenditaCreate(BaseModel):
    quantita: float
    data_vendita: date
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None
    # Prezzo al momento della vendita: se non specificato, il backend lo
    # ricava dal listino (ricette/articoli) al momento dell'inserimento.
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None

class VenditaUpdate(BaseModel):
    quantita: Optional[float] = None
    data_vendita: Optional[date] = None
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None

class VenditaBulkItem(BaseModel):
    """Singola riga di vendita nell'array bulk proveniente dall'AI Scanner."""
    data_vendita: date
    quantita: float
    id_prodotto_menu: Optional[str] = None
    id_tipo: str  # "finito" | "commerciale" | "sospeso"
    nome_vendita: Optional[str] = None
    # Prezzo estratto dallo scontrino/excel, se presente (unitario e/o totale
    # riga). Se entrambi assenti, il backend usa il prezzo di listino attuale.
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None
    # True se prezzo_singolo/prezzo_totale sono LORDI (IVA inclusa), come
    # stampati su uno scontrino/comanda: il backend li converte in netto usando
    # iva_percentuale se nota, altrimenti l'aliquota IVA del prodotto associato.
    prezzo_lordo: bool = False
    # Aliquota IVA rilevata direttamente sul documento sorgente (scontrino),
    # se indicata esplicitamente. Ha priorità sull'aliquota di listino.
    iva_percentuale: Optional[float] = None

class VenditaBulkPayload(BaseModel):
    items: List[VenditaBulkItem]

class VenditaBulkDelete(BaseModel):
    ids: List[int]

class VenditaBulkPrezzoUpdate(BaseModel):
    """Modifica in blocco del prezzo di vendita su un insieme di vendite già
    registrate (stesso prodotto, giorni diversi) — usata dallo strumento di
    modifica prezzi in Registro Vendite. Tutti i valori sono LORDI (IVA
    inclusa, quelli che l'utente conosce/vede davvero, es. da scontrino o
    listino): il netto si ottiene scorporando l'aliquota IVA del prodotto.

    Esattamente uno tra i due campi va valorizzato:
    - nuovo_prezzo_singolo_lordo: stesso prezzo UNITARIO applicato a ogni riga
      selezionata; il totale di ciascuna segue dalla sua quantità già salvata
      (righe con quantità diverse ottengono totali diversi, stesso unitario).
    - nuovo_totale_lordo: stesso TOTALE applicato a ogni riga selezionata
      (utile quando si conosce l'incasso complessivo di quel prodotto in quel
      giorno, es. da un report di cassa); il prezzo unitario si ricava da
      quello dividendo per la quantità già salvata su ciascuna riga.
    """
    ids: List[int]
    nuovo_prezzo_singolo_lordo: Optional[float] = None
    nuovo_totale_lordo: Optional[float] = None

    @model_validator(mode="after")
    def check_esattamente_uno_dei_due(self):
        if (self.nuovo_prezzo_singolo_lordo is None) == (self.nuovo_totale_lordo is None):
            raise ValueError("Specifica esattamente uno tra prezzo unitario e totale (lordi).")
        return self

class ImportedVendita(BaseModel):
    nome_prodotto_estratto: str
    quantita: float
    data_vendita: date
    # Sempre il totale LORDO (IVA inclusa) della riga — il prompt in
    # utils/ai_parser.py (parse_vendite_excel_with_ai_stream) impone che sia
    # SEMPRE valorizzato, mai null. Campo obbligatorio (non Optional) apposta:
    # response_schema vincola l'output di Gemini a questa forma, quindi deve
    # corrispondere esattamente al prompt o la costrizione dello schema
    # vincerebbe sulle istruzioni testuali. Il prezzo unitario lordo e i
    # valori netto NON arrivano dal modello: si derivano nel codice
    # (prezzo_singolo_lordo = prezzo_totale / quantita a schermo, poi lo
    # scorporo IVA vero e proprio avviene in registra_vendite_bulk una volta
    # noto il prodotto abbinato e la sua aliquota).
    prezzo_totale: float

class ParsedVenditaResult(BaseModel):
    vendite: List[ImportedVendita]

class ScontrinoItem(BaseModel):
    """Una voce estratta da uno scontrino/comanda dallo scanner AI (vedi
    routers/ai_scanner.py). Usato come response_schema per Gemini: prima di
    questa modifica lo scanner era l'unico dei tre flussi AI (scanner,
    import Excel vendite, import fatture) a fidarsi del JSON grezzo restituito
    dal modello senza alcun vincolo di schema — un output malformato o con
    campi mancanti arrivava fino alla validazione di /api/vendite/bulk, l'unico
    vero punto di controllo. data_vendita resta una stringa (non `date`):
    l'AI può legittimamente non trovarla sul documento e lasciarla null,
    caso normale gestito dall'utente in fase di revisione, non un errore da
    respingere qui."""
    id_documento: str
    data_vendita: Optional[str] = None
    nome_rilevato: str
    id_prodotto_menu: Optional[str] = None
    quantita: float
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None
    iva_percentuale: Optional[float] = None

class VenditaSospesaResponse(BaseModel):
    id: str
    nome_vendita: str
    quantita: float
    data_vendita: date
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None

class VenditaSospesaResolve(BaseModel):
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None
    # Correzioni opzionali: se non specificate, si usano quantita/data_vendita/
    # prezzo_totale (LORDO) già salvati sulla riga sospesa.
    quantita: Optional[float] = None
    data_vendita: Optional[date] = None
    # Totale LORDO corretto dall'utente: se presente, sostituisce il totale
    # grezzo importato. Il prezzo unitario lordo si ricava dividendo per la
    # quantità finale (stesso principio di VenditaBulkPrezzoUpdate.nuovo_totale_lordo).
    prezzo_totale_lordo: Optional[float] = None
