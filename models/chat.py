from pydantic import BaseModel, field_validator

STATI_VALIDI = ("aperta", "risolta")


class ChatCreate(BaseModel):
    oggetto: str
    messaggio: str

    @field_validator("oggetto")
    @classmethod
    def check_oggetto(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("L'oggetto è obbligatorio")
        return v

    @field_validator("messaggio")
    @classmethod
    def check_messaggio(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il messaggio è obbligatorio")
        return v


class MessaggioCreate(BaseModel):
    testo: str

    @field_validator("testo")
    @classmethod
    def check_testo(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Il messaggio è obbligatorio")
        return v


class ChatStatoUpdate(BaseModel):
    stato: str

    @field_validator("stato")
    @classmethod
    def check_stato(cls, v: str) -> str:
        if v not in STATI_VALIDI:
            raise ValueError(f"Stato non valido, deve essere uno tra: {', '.join(STATI_VALIDI)}")
        return v
