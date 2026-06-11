from pydantic import BaseModel
from typing import Optional
from uuid import UUID

class CategoriaCostoCreate(BaseModel):
    nome_categoria: str
    is_fisso: bool 
    
class CostoAnnoMeseCreate(BaseModel):
    id_categoria: str 
    mese: str
    anno: int
    importo: float
    note: Optional[str] = None

class CategoriaCostoUpdate(BaseModel):
    nome_categoria: Optional[str] = None
    is_fisso: Optional[bool] = None
    
class CostoAnnoMeseUpdate(BaseModel):
    id_categoria: Optional[str] = None
    mese: Optional[str] = None
    anno: Optional[int] = None
    importo: Optional[float] = None
    note: Optional[str] = None