from pydantic import BaseModel
from typing import Optional

class CategoriaProdottoCreate(BaseModel):
    nome_categoria: str
    id_macro_categoria: Optional[int] = None

class CategoriaProdottoUpdate(BaseModel):
    nome_categoria: Optional[str] = None
    id_macro_categoria: Optional[int] = None

class MateriaPrimaCreate(BaseModel):
    articolo: str
    unita_misura: str
    costo_netto: float
    fornitore: Optional[str] = None
    partita_iva: Optional[str] = None
    anno: Optional[int] = None

class MateriaPrimaUpdate(BaseModel):
    articolo: Optional[str] = None
    unita_misura: Optional[str] = None
    costo_netto: Optional[float] = None
    fornitore: Optional[str] = None
    partita_iva: Optional[str] = None
    anno: Optional[int] = None


class ProdottoRivenditaCreate(BaseModel):
    nome_articolo: str
    unita_misura: str
    food_cost: float
    prezzo_vendita: float
    id_categoria_prodotto: int


class ProdottoRivenditaUpdate(BaseModel):
    nome_articolo: Optional[str] = None
    unita_misura: Optional[str] = None
    food_cost: Optional[float] = None
    prezzo_vendita: Optional[float] = None
    stock: Optional[int] = None
    id_categoria_prodotto: Optional[int] = None