"""
Helper per parlare con Supabase riducendo la latenza sommata dei round trip.

Contesto: ogni .execute() è una richiesta HTTP (~60-120 ms). Gli endpoint che
fanno 3-7 fetch INDIPENDENTI in sequenza pagano la somma delle latenze; qui le
facciamo partire insieme. Il client httpx condiviso (vedi database/config.py)
è thread-safe e ha un pool di connessioni dimensionato apposta.
"""
import time
import concurrent.futures

from database.config import Database

# Disponibilità delle funzioni SQL (vedi sql/003_statistiche_rpc.sql): se una
# funzione non esiste ancora sul database, lo ricordiamo per un po' invece di
# ritentare (e fallire) ad ogni richiesta. Ricontrolliamo periodicamente, così
# quando l'utente esegue lo script SQL le funzioni entrano in uso da sole,
# senza riavviare il backend.
_RPC_STATE: dict = {}
_RPC_RECHECK_TTL = 120  # secondi


def call_rpc_or_none(nome: str, params: dict, order_cols: list = None, page_size: int = 1000, max_workers: int = 4):
    """
    Chiama una funzione SQL (RPC) e ritorna TUTTE le righe. Ritorna None se la
    funzione non esiste ancora o se la chiamata fallisce per qualsiasi motivo:
    il chiamante DEVE avere un percorso alternativo equivalente (fallback
    Python). Mai un errore all'utente per una funzione SQL mancante.

    PostgREST tronca anche i risultati delle funzioni al limite di righe del
    progetto (1000 di default) — un troncamento SILENZIOSO che produce totali
    sbagliati, non un errore. Per questo qui si pagina sempre, con count
    esatto sulla prima pagina e le successive in parallelo.

    order_cols è OBBLIGATORIO appena il risultato può superare una pagina: è
    PostgREST a ordinare l'output della funzione prima di applicare il range
    (prefisso "-" per discendente), e senza un ordinamento deterministico le
    pagine di richieste separate possono sovrapporsi/perdere righe. Usare le
    colonne del GROUP BY della funzione (combinazione unica).
    """
    state = _RPC_STATE.get(nome)
    if state and state[0] is False and (time.time() - state[1]) < _RPC_RECHECK_TTL:
        return None
    try:
        def q(with_count):
            b = Database.get_client().rpc(nome, params, count="exact" if with_count else None)
            for col in (order_cols or []):
                if col.startswith("-"):
                    b = b.order(col[1:], desc=True)
                else:
                    b = b.order(col)
            return b

        first = q(True).range(0, page_size - 1).execute()
        rows = list(first.data or [])
        total = first.count if first.count is not None else len(rows)

        if total > page_size and len(rows) == page_size:
            ranges = [(start, min(start + page_size - 1, total - 1))
                      for start in range(page_size, total, page_size)]

            def _fetch(rng):
                return q(False).range(rng[0], rng[1]).execute().data or []

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                for chunk in ex.map(_fetch, ranges):
                    rows.extend(chunk)

        _RPC_STATE[nome] = (True, time.time())

        # Guardia: se per qualunque motivo non abbiamo TUTTE le righe promesse
        # dal count, meglio il fallback (corretto) di un totale monco. Non
        # marchiamo la funzione come assente: è un problema transitorio.
        if first.count is not None and len(rows) != first.count:
            return None
        return rows
    except Exception:
        _RPC_STATE[nome] = (False, time.time())
        return None


def run_parallel(*fns):
    """
    Esegue N callable (ognuna tipicamente una query .execute()) in parallelo e
    ritorna i risultati nello stesso ordine. Le eccezioni della singola callable
    vengono rilanciate al chiamante, come se fosse stata eseguita inline.
    """
    if len(fns) == 1:
        return [fns[0]()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futures = [ex.submit(fn) for fn in fns]
        return [f.result() for f in futures]


def fetch_all_parallel(make_query, page_size=1000, max_workers=4, order_col="id"):
    """
    Scarica TUTTE le righe di una query paginata. La prima pagina viaggia con
    count esatto, così le pagine restanti (note in anticipo) partono in
    parallelo invece che in sequenza — su tabelle da N pagine il tempo passa
    da N round trip sommati a ~2.

    make_query(with_count: bool) deve ritornare un builder Supabase NUOVO già
    filtrato (i builder non sono riutilizzabili); quando with_count è True la
    select va costruita con count="exact".

    IMPORTANTE: l'ordinamento per order_col è OBBLIGATORIO e viene applicato
    qui. Senza un ORDER BY stabile, Postgres è libero di restituire le righe
    in ordine diverso ad ogni richiesta (specie con scan paralleli): pagine
    chieste come richieste HTTP separate finirebbero per sovrapporsi e
    perdere righe — totali silenziosamente sbagliati, non un errore visibile.
    """
    first = make_query(True).order(order_col).range(0, page_size - 1).execute()
    rows = list(first.data or [])
    total = first.count if first.count is not None else len(rows)
    if total <= page_size or len(rows) < page_size:
        return rows

    ranges = [(start, min(start + page_size - 1, total - 1))
              for start in range(page_size, total, page_size)]

    def _fetch(rng):
        return make_query(False).order(order_col).range(rng[0], rng[1]).execute().data or []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for chunk in ex.map(_fetch, ranges):
            rows.extend(chunk)
    return rows
