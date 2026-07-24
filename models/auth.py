import re
from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
from uuid import UUID


def validate_password_strength(value: str) -> str:
    """
    Regola di complessità comune a registrazione e reset password: minimo 6
    caratteri, una maiuscola, un numero e un carattere speciale. Non si
    applica al login, che valida una password già esistente.
    """
    if len(value) < 6:
        raise ValueError("La password deve avere almeno 6 caratteri")
    if not re.search(r"[A-Z]", value):
        raise ValueError("La password deve contenere almeno una lettera maiuscola")
    if not re.search(r"[0-9]", value):
        raise ValueError("La password deve contenere almeno un numero")
    if not re.search(r"[^A-Za-z0-9]", value):
        raise ValueError("La password deve contenere almeno un carattere speciale")
    return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserRegister(BaseModel):
    email: EmailStr
    password: str
    nome: str
    cognome: str
    cellulare: str
    id_sede: Optional[UUID]  = None
    # Il ruolo NON è un campo scelto da chi si registra: ogni nuova utenza
    # nasce sempre come "user" (id 2 nella tabella roles). Un eventuale
    # admin va promosso in un secondo momento, non auto-assegnato in fase
    # di registrazione.

    @field_validator("password")
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return validate_password_strength(v)

    @field_validator("cellulare")
    @classmethod
    def check_cellulare(cls, v: str) -> str:
        v = v.strip()
        if len(re.sub(r"\D", "", v)) < 6:
            raise ValueError("Numero di cellulare non valido")
        return v

class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ForgotPassword(BaseModel):
    email: EmailStr

class UpdatePassword(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return validate_password_strength(v)

class UpdateUser(BaseModel):
    nome: Optional[str] = None
    cognome: Optional[str] = None
    telefono: Optional[str] = None