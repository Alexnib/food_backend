from models.vendite import ParsedVenditaResult
import json

final_json = json.dumps({"vendite": [{"nome_prodotto_estratto": "test", "quantita": 1.0, "data_vendita": "2026-07-13"}]})
try:
    ParsedVenditaResult.model_validate_json(final_json)
    print("SUCCESS")
except Exception as e:
    print("ERROR:", str(e))
