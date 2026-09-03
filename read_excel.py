import pandas as pd
try:
    df = pd.read_excel(r'C:\Users\MIDHUN\Downloads\Product List.xlsx', nrows=0)
    print("COLUMNS:", df.columns.tolist())
except Exception as e:
    print("ERROR:", e)
