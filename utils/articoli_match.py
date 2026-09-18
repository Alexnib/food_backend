"""
Helper per riconoscere se un articolo importato (da fattura o Excel) esiste
già a catalogo, e per la data di un prezzo d'acquisto.

La chiave di confronto è (nome normalizzato, unità di misura normalizzata):
nome senza differenze di maiuscole/spazi, unità con gli alias più comuni
uniti ("l" = "lt", "gr" = "g"). L'unità fa parte della chiave perché lo
stesso nome con unità diverse (kg / pz) non è confrontabile come prezzo.

ATTENZIONE: src/lib/articoli.ts (frontend) replica identica questa
normalizzazione per mostrare il badge "esistente" nella schermata di
revisione dell'import — se cambia qui, va cambiata anche lì.
"""
import datetime

_ALIAS_UNITA = {"l": "lt", "lt.": "lt", "gr": "g", "gr.": "g", "kg.": "kg", "pz.": "pz"}


def normalizza_nome(nome) -> str:
    return " ".join(str(nome or "").casefold().split())


def normalizza_unita(unita) -> str:
    u = str(unita or "").strip().casefold()
    return _ALIAS_UNITA.get(u, u)


def chiave_articolo(nome, unita) -> tuple:
    return (normalizza_nome(nome), normalizza_unita(unita))


def data_prezzo_valida(valore) -> datetime.date:
    """Data (ISO YYYY-MM-DD) di un prezzo d'acquisto: quella della fattura se
    presente e valida, altrimenti oggi. Una data futura non è credibile per
    un acquisto già avvenuto (sarebbe una lettura sbagliata dell'AI): oggi."""
    oggi = datetime.date.today()
    try:
        data = datetime.date.fromisoformat(str(valore))
    except ValueError:
        return oggi
    return min(data, oggi)
