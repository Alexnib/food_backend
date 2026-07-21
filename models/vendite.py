from pydantic import BaseModel
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
    modifica prezzi in Registro Vendite."""
    ids: List[int]
    nuovo_prezzo_singolo: float

class ImportedVendita(BaseModel):
    nome_prodotto_estratto: str
    quantita: float
    data_vendita: date
    # Presenti solo se il file importato (Excel/CSV) contiene una colonna di
    # prezzo riconoscibile: unitario e/o totale di riga.
    prezzo_singolo: Optional[float] = None
    prezzo_totale: Optional[float] = None
    # True se l'IA ha stimato che i prezzi della colonna sono LORDI (IVA
    # inclusa) invece che netti di listino; None se non è stato estratto
    # alcun prezzo. Guida la conversione in netto lato backend.
    prezzo_lordo: Optional[bool] = None

class ParsedVenditaResult(BaseModel):
    vendite: List[ImportedVendita]

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
