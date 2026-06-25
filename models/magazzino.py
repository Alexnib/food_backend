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
    prezzo_acquisto_lordo: float
    prezzo_acquisto_netto: float
    id_iva_acquisto: int
    fornitore: Optional[str] = None
    partita_iva: Optional[str] = None
    anno: Optional[int] = None

class MateriaPrimaUpdate(BaseModel):
    articolo: Optional[str] = None
    unita_misura: Optional[str] = None
    prezzo_acquisto_lordo: Optional[float] = None
    prezzo_acquisto_netto: Optional[float] = None
    id_iva_acquisto: Optional[int] = None
    fornitore: Optional[str] = None
    partita_iva: Optional[str] = None
    anno: Optional[int] = None

class ProdottoRivenditaCreate(BaseModel):
    nome_articolo: str
    unita_misura: str
    prezzo_acquisto_lordo: float
    prezzo_acquisto_netto: float
    id_iva_acquisto: int
    prezzo_vendita_lordo: float
    prezzo_vendita_netto: float
    id_iva_rivendita: int
    id_categoria_prodotto: int

class ProdottoRivenditaUpdate(BaseModel):
    nome_articolo: Optional[str] = None
    unita_misura: Optional[str] = None
    prezzo_acquisto_lordo: Optional[float] = None
    prezzo_acquisto_netto: Optional[float] = None
    id_iva_acquisto: Optional[int] = None
    prezzo_vendita_lordo: Optional[float] = None
    prezzo_vendita_netto: Optional[float] = None
    id_iva_rivendita: Optional[int] = None
    stock: Optional[int] = None
    id_categoria_prodotto: Optional[int] = None