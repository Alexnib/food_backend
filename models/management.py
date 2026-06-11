from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID

class CreateNegozio(BaseModel):
    nome_negozio: str
    partita_iva: str
    
class CreateSede(BaseModel):
    id_negozio: UUID
    indirizzo: str
    comune: str
    civico: str
    nome_responsabile: str
    cognome_responsabile: str