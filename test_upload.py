import requests
import io
import pandas as pd

df = pd.DataFrame({
    "Prodotto": ["Pizza Margherita", "Coca Cola"],
    "Quantita": [2, 1],
    "Data": ["2026-07-13", "2026-07-13"]
})
excel_io = io.BytesIO()
df.to_excel(excel_io, index=False)
excel_bytes = excel_io.getvalue()

files = {'file': ('test.xlsx', excel_bytes, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
resp = requests.post('http://localhost:8000/api/vendite/import/upload', files=files, stream=True)
print("Status:", resp.status_code)
for line in resp.iter_lines():
    if line:
        print(line.decode('utf-8'))
