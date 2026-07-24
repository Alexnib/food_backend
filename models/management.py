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


class UpdateMiaSede(BaseModel):
    """Modifica dei dati della propria sede/negozio dal profilo (non
    riassegnazione a un'altra sede: solo i campi impostati in onboarding)."""
    nome_negozio: Optional[str] = None
    partita_iva: Optional[str] = None
    indirizzo: Optional[str] = None
    comune: Optional[str] = None
    civico: Optional[str] = None