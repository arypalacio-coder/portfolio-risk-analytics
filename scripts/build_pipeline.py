# -*- coding: utf-8 -*-
import duckdb
import pandas as pd
import numpy as np
import yfinance as yf
import os

print(">>> [1/5] Descargando datos de mercado via yfinance (sin concurrencia de cache)...")
TICKERS = ["SPY", "QQQ", "EEM", "TLT", "BND", "GLD", "BIL"]

# Descarga secuencial limpia para evitar bloqueos de sqlite interno de yfinance
price_frames = []
for ticker in TICKERS:
    t_df = yf.download(ticker, start="2021-01-01", end="2026-09-01", progress=False, threads=False)
    if "Close" in t_df:
        s = t_df["Close"].squeeze()
        df_single = pd.DataFrame({"calendar_date": s.index, "adj_close": s.values, "ticker": ticker})
        price_frames.append(df_single)

prices_long = pd.concat(price_frames, ignore_index=True)
prices_long["calendar_date"] = pd.to_datetime(prices_long["calendar_date"]).dt.date

os.makedirs("data/processed", exist_ok=True)
db_path = "portfolio.duckdb"

print(">>> [2/5] Conectando a DuckDB y estructurando dimensiones...")
con = duckdb.connect(db_path)

# 1. dim_asset
con.execute("""
CREATE OR REPLACE TABLE dim_asset AS
SELECT * FROM (VALUES
    (1, 'SPY', 'SPDR S&P 500 ETF Trust', 'Equity', 'United States', 'Large Cap Blend', 'State Street'),
    (2, 'QQQ', 'Invesco QQQ Trust', 'Equity', 'United States', 'Technology / Large Growth', 'Invesco'),
    (3, 'EEM', 'iShares MSCI Emerging Markets ETF', 'Equity', 'Emerging Markets', 'Broad Emerging', 'BlackRock'),
    (4, 'TLT', 'iShares 20+ Year Treasury Bond ETF', 'Fixed Income', 'United States', 'Long-Term Sovereign', 'BlackRock'),
    (5, 'BND', 'Vanguard Total Bond Market ETF', 'Fixed Income', 'United States', 'Aggregate Bond', 'Vanguard'),
    (6, 'GLD', 'SPDR Gold Shares', 'Commodity', 'Global', 'Precious Metals', 'State Street'),
    (7, 'BIL', 'SPDR Bloomberg 1-3 Month T-Bill ETF', 'Cash Equivalent', 'United States', 'Ultra Short Sovereign', 'State Street')
) AS t(asset_key, ticker, asset_name, asset_class, geography, sector, issuer);
""")

# 2. dim_benchmark
con.execute("""
CREATE OR REPLACE TABLE dim_benchmark AS
SELECT * FROM (VALUES
    (1, 'SPY', 'S&P 500 Benchmark Index', 'US Broad Market Benchmark'),
    (2, 'QQQ', 'NASDAQ-100 Benchmark Index', 'US Large Growth & Tech Benchmark')
) AS t(bench_key, ticker, bench_name, description);
""")

# 3. dim_date
con.execute("""
CREATE OR REPLACE TABLE dim_date AS
WITH date_series AS (
    SELECT CAST(range AS DATE) AS calendar_date
    FROM range(DATE '2021-01-01', DATE '2026-09-02', INTERVAL 1 DAY)
)
SELECT 
    CAST(strftime(calendar_date, '%Y%m%d') AS INTEGER) AS date_key,
    calendar_date,
    EXTRACT(YEAR FROM calendar_date)::INTEGER AS year,
    EXTRACT(QUARTER FROM calendar_date)::INTEGER AS quarter,
    EXTRACT(MONTH FROM calendar_date)::INTEGER AS month,
    EXTRACT(DAY FROM calendar_date)::INTEGER AS day,
    strftime(calendar_date, '%Y-%m') AS year_month,
    CASE WHEN dayofweek(calendar_date) IN (0, 6) THEN FALSE ELSE TRUE END AS is_weekday
FROM date_series;
""")

con.register("df_prices_raw", prices_long)

print(">>> [3/5] Calculando retornos diarios en fct_asset_prices...")
con.execute("""
CREATE OR REPLACE TABLE fct_asset_prices AS
WITH raw_data AS (
    SELECT 
        d.date_key,
        a.asset_key,
        p.calendar_date,
        p.ticker,
        p.adj_close
    FROM df_prices_raw p
    JOIN dim_asset a ON p.ticker = a.ticker
    JOIN dim_date d ON p.calendar_date = d.calendar_date
    WHERE p.adj_close IS NOT NULL
),
returns_calc AS (
    SELECT 
        date_key,
        asset_key,
        adj_close,
        adj_close / LAG(adj_close) OVER (PARTITION BY asset_key ORDER BY date_key) - 1 AS return_1d
    FROM raw_data
)
SELECT 
    row_number() OVER () AS price_id,
    date_key,
    asset_key,
    adj_close,
    COALESCE(return_1d, 0.0) AS return_1d
FROM returns_calc;
""")

print(">>> [4/5] Generando ledger transaccional determinista...")
con.execute("""
CREATE OR REPLACE TABLE fct_transactions AS
SELECT * FROM (VALUES
    (1, 20210104, 7, 'DEPOSIT', 0.0, 100.0, 10000000.0, 'Initial capital injection'),
    (2, 20210104, 1, 'BUY', 10500.0, 370.0, -3885000.0, 'Initial allocation SPY (40%)'),
    (3, 20210104, 2, 'BUY', 6300.0, 313.0, -1971900.0, 'Initial allocation QQQ (20%)'),
    (4, 20210104, 4, 'BUY', 9500.0, 157.0, -1491500.0, 'Initial allocation TLT (15%)'),
    (5, 20210104, 5, 'BUY', 11500.0, 88.0, -1012000.0, 'Initial allocation BND (10%)'),
    (6, 20210104, 6, 'BUY', 5300.0, 182.0, -964600.0, 'Initial allocation GLD (10%)'),
    (7, 20210104, 7, 'BUY', 6750.0, 100.0, -675000.0, 'Cash reserve BIL (5%)'),
    (8, 20220615, 1, 'BUY', 2500.0, 379.0, -947500.0, 'Rebalancing pull-back buy SPY'),
    (9, 20220615, 7, 'SELL', -9475.0, 100.0, 947500.0, 'Funding SPY from cash reserves'),
    (10, 20230110, 2, 'BUY', 3000.0, 275.0, -825000.0, 'Strategic expansion tech allocation QQQ'),
    (11, 20240315, 6, 'SELL', -1500.0, 200.0, 300000.0, 'Tactical profit taking on GLD'),
    (12, 20240315, 7, 'BUY', 3000.0, 100.0, -300000.0, 'Sweep profit to cash buffer'),
    (13, 20250210, 5, 'BUY', 4000.0, 73.0, -292000.0, 'Bond duration lock BND'),
    (14, 20260115, 7, 'DEPOSIT', 0.0, 100.0, 2000000.0, 'Institutional top-up cash injection')
) AS t(txn_id, date_key, asset_key, txn_type, units, price, amount, notes);
""")

con.execute("""
CREATE OR REPLACE TABLE fct_daily_positions AS
WITH asset_dates AS (
    SELECT DISTINCT p.date_key, a.asset_key
    FROM fct_asset_prices p
    CROSS JOIN dim_asset a
),
cum_txns AS (
    SELECT 
        d.date_key,
        t.asset_key,
        SUM(t.units) AS delta_units
    FROM fct_transactions t
    JOIN dim_date d ON t.date_key = d.date_key
    GROUP BY d.date_key, t.asset_key
),
expanded_units AS (
    SELECT 
        ad.date_key,
        ad.asset_key,
        COALESCE(c.delta_units, 0.0) AS delta_units
    FROM asset_dates ad
    LEFT JOIN cum_txns c ON ad.date_key = c.date_key AND ad.asset_key = c.asset_key
),
position_history AS (
    SELECT 
        date_key,
        asset_key,
        SUM(delta_units) OVER (PARTITION BY asset_key ORDER BY date_key ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS units
    FROM expanded_units
)
SELECT 
    row_number() OVER () AS pos_id,
    ph.date_key,
    ph.asset_key,
    ph.units,
    ph.units * pr.adj_close AS market_value
FROM position_history ph
JOIN fct_asset_prices pr ON ph.date_key = pr.date_key AND ph.asset_key = pr.asset_key
WHERE ph.units > 0;
""")

print(">>> [5/5] Exportando tablas auditadas a Parquet (Data Freeze)...")
tables = ["dim_asset", "dim_benchmark", "dim_date", "fct_asset_prices", "fct_transactions", "fct_daily_positions"]
for t in tables:
    output_path = f"data/processed/{t}.parquet"
    con.execute(f"COPY {t} TO '{output_path}' (FORMAT PARQUET);")
    print(f"    Exportado -> {output_path}")

con.close()
print(">>> Pipeline DuckDB completado exitosamente. Conexion con.close() liberada.")
