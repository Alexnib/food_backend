from pydantic import BaseModel
from typing import Optional, List

# --- INGREDIENTI (Da inserire dentro la ricetta) ---
class IngredienteRicettaItem(BaseModel):
    id_materia_prima: str
    quantita_per_kg: float
    perc_scarto: float = 0.0

# --- RICETTA ---
class RicettaCreate(BaseModel):
    nome_ricetta: str
    descrizione_ricetta: Optional[str] = None
    id_categoria_prodotto: Optional[int] = None
    ingredienti: List[IngredienteRicettaItem] # Riceviamo la lista in un colpo solo!
    prezzo_vendita_lordo: float = 0.0
    prezzo_vendita_netto: float = 0.0
    id_iva_vendita: int


# --- IMPORT RICETTE DA EXCEL (AI) ---
# Vedi utils/ai_parser.py, parse_ricette_excel_with_ai_stream: a differenza
# dell'import materie prime, qui l'AI non risolve l'ingrediente a un id del
# catalogo (rischio di allucinazione) — restituisce solo il testo estratto,
# l'abbinamento a un id_materia_prima reale avviene lato frontend (fuzzy
# matching, vedi src/utils/prodottoMatching.ts) prima del salvataggio.
class ParsedIngredienteRicetta(BaseModel):
    nome_ingrediente_estratto: str
    quantita: float

class ParsedRicetta(BaseModel):
    # Riferimento interno (indice del blocco nel chunk, vedi
    # _correggi_quantita_con_originali in ai_parser.py): l'AI deve
    # riportarlo invariato, MAI usarlo per abbinare il nome. Serve a
    # ricollegare con certezza ogni ricetta restituita al blocco originale
    # da cui è partita — abbinare per nome falliva silenziosamente ogni
    # volta che l'AI "ripuliva" il nome anche di un solo carattere.
    id_blocco: int
    nome_ricetta: str
    id_categoria: Optional[int] = None
    # Presenti solo se l'utente ha dichiarato che il file contiene un
    # prezzo di vendita (vedi prezzo_vendita_presente in
    # parse_ricette_excel_with_ai_stream) — altrimenti sempre null.
    prezzo_vendita_netto: Optional[float] = None
    prezzo_vendita_lordo: Optional[float] = None
    ingredienti: List[ParsedIngredienteRicetta]

class ParsedRicetteResult(BaseModel):
    ricette: List[ParsedRicetta]

# --- RICETTE SOSPESE (import parzialmente completato) ---
# Stessa forma di IngredienteImportato/RicettaImportata lato frontend:
# id_materia_prima è QUI opzionale (a differenza di IngredienteRicettaItem)
# perché una riga sospesa può avere ingredienti ancora non abbinati a un
# articolo reale — è esattamente il motivo per cui la ricetta è "sospesa"
# invece di poter essere salvata subito come RicettaCreate.
class IngredienteSospesoItem(BaseModel):
    nome_ingrediente_estratto: str
    quantita: float
    id_materia_prima: Optional[str] = None
    perc_scarto: float = 0.0

class RicettaSospesaInput(BaseModel):
    nome_ricetta: str = ""
    id_categoria_prodotto: Optional[int] = None
    prezzo_vendita_netto: float = 0.0
    prezzo_vendita_lordo: float = 0.0
    id_iva_vendita: Optional[int] = None
    ingredienti: List[IngredienteSospesoItem] = []

# Corpo di /api/produzione/import/save: le ricette pronte (ingredienti già
# risolti a id_materia_prima reali, stessa forma esatta di RicettaCreate) e
# quelle ancora incomplete, che finiscono in ricette_sospese invece di
# essere perse — vedi sql/017_ricette_sospese.sql.
class SaveImportRicetteRequest(BaseModel):
    ricette: List[RicettaCreate] = []
    ricette_sospese: List[RicettaSospesaInput] = []