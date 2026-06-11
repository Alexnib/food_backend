from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserRegister(BaseModel):
    email: EmailStr
    password: str
    nome: str
    cognome: str
    cellulare: Optional[str] = None
    id_sede: Optional[UUID]  = None
    role: Optional[str] = "staff"
    
class RefreshTokenRequest(BaseModel):
    refresh_token: str
    
    
class ForgotPassword(BaseModel):
    email: EmailStr

class UpdatePassword(BaseModel):
    new_password: str

class UpdateUser(BaseModel):
    nome: Optional[str] = None
    cognome: Optional[str] = None
    telefono: Optional[str] = None