from pydantic import BaseModel, Field
from typing import Optional, List, Union

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

class ImportedProduct(BaseModel):
    nome_prodotto: str = Field(description="Il nome del prodotto pulito")
    tipo: str = Field(description="Deve essere rigorosamente 'Materia Prima' oppure 'Rivendita'")
    unita_misura: str = Field(description="L'unità di misura, es. kg, lt, pz, gr")
    costo_netto: float = Field(description="Il costo al netto dell'IVA (float)")
    iva_perc: int = Field(description="La percentuale di IVA (es. 4, 10, 22 o 0)")
    costo_lordo: float = Field(description="Il costo comprensivo di IVA (float)")
    id_categoria: Optional[int] = Field(None, description="L'ID della categoria più adatta tra quelle fornite")

class ParsedResult(BaseModel):
    prodotti: List[ImportedProduct]

class ImportItem(BaseModel):
    nome_prodotto: str
    tipo: str
    unita_misura: str
    costo_netto: float
    iva_perc: int
    costo_lordo: float
    id_categoria: Optional[Union[int, str]] = None

class SaveImportRequest(BaseModel):
    prodotti: List[ImportItem]