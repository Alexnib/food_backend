from pydantic import BaseModel
from typing import Optional, List

# --- INGREDIENTI (Da inserire dentro la ricetta) ---
class IngredienteRicettaItem(BaseModel):
    id_materia_prima: str
    quantita_per_kg: float
    perc_scarto: float = 0.0

# --- RICETTA ---
class RicettaCreate(BaseModel):
    nome_ricetta: str
    descrizione_ricetta: Optional[str] = None
    id_categoria_prodotto: Optional[int] = None
    ingredienti: List[IngredienteRicettaItem] # Riceviamo la lista in un colpo solo!
    prezzo_vendita_lordo: float = 0.0
    prezzo_vendita_netto: float = 0.0
    id_iva_vendita: int