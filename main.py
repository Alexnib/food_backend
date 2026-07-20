from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.test import router as test
from routers.managment import router as management 
from routers.auth import router as auth
from routers.contabilita import router as contabilita
from routers.magazzino import router as magazzino
from routers.produzione import router as produzione
from routers.vendite import router as vendite
from routers.statistiche import router as statistiche
from routers.ai_scanner import router as ai_scanner
from routers.admin import router as admin
from routers import import_magazzino

app = FastAPI(title="Gestionale Food API")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,    
    allow_methods=["*"],        
    allow_headers=["*"],         
)


app.include_router(test)
app.include_router(management)
app.include_router(auth)
app.include_router(contabilita)
app.include_router(magazzino)
app.include_router(produzione)
app.include_router(vendite)
app.include_router(statistiche)
app.include_router(ai_scanner)
app.include_router(admin)
app.include_router(import_magazzino.router)

# Endpoint di base per verificare che il server sia acceso
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Server FastAPI in esecuzione correttamente!"}