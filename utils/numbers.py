def round2(value):
    """Arrotonda a 2 decimali un valore numerico prima che finisca a DB, per
    evitare che divisioni/moltiplicazioni (es. conversioni netto/lordo IVA,
    prezzo_totale / quantita) salvino numeri periodici o con troppi decimali
    nelle tabelle vendite, vendite_sospese, articoli e ricette."""
    return round(value, 2) if value is not None else None
