from pydantic import BaseModel
from typing import Optional, List
from datetime import date

class VenditaCreate(BaseModel):
    quantita: float
    data_vendita: date
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None

class VenditaUpdate(BaseModel):
    quantita: Optional[float] = None
    data_vendita: Optional[date] = None
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None

class VenditaBulkItem(BaseModel):
    """Singola riga di vendita nell'array bulk proveniente dall'AI Scanner."""
    data_vendita: date
    quantita: float
    id_prodotto_menu: Optional[str] = None
    id_tipo: str  # "finito" | "commerciale" | "sospeso"
    nome_vendita: Optional[str] = None

class VenditaBulkPayload(BaseModel):
    items: List[VenditaBulkItem]

class VenditaBulkDelete(BaseModel):
    ids: List[int]
class ImportedVendita(BaseModel):
    nome_prodotto_estratto: str
    quantita: float
    data_vendita: date

class ParsedVenditaResult(BaseModel):
    vendite: List[ImportedVendita]

class VenditaSospesaResponse(BaseModel):
    id: str
    nome_vendita: str
    quantita: float
    data_vendita: date

class VenditaSospesaResolve(BaseModel):
    id_ricetta: Optional[str] = None
    id_prodotto_commerciale: Optional[str] = None
