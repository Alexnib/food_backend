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
    id_prodotto_menu: str
    id_tipo: str  # "finito" | "commerciale"

class VenditaBulkPayload(BaseModel):
    items: List[VenditaBulkItem]

class VenditaBulkDelete(BaseModel):
    ids: List[int]