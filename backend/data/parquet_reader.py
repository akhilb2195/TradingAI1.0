import pandas as pd

df = pd.read_parquet("backend/data/ticks_archive/ticks_2026-07-06.parquet")

# df = pd.read_parquet(file_path)

print(df.tail(1))