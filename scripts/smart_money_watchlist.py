"""
watchlist.py

Atul's tracked universe (104 tickers). Kept as a standalone module so it can
be imported independently or swapped out without touching scanner logic.
"""

WATCHLIST = [
    "AA", "AAPL", "ADBE", "AMD", "AMZN", "APA", "AR", "AVGO", "AXP", "BA",
    "BABA", "BAC", "BMY", "BP", "BSX", "BX", "C", "CCJ", "CMG", "COF",
    "COIN", "CRM", "CSCO", "CSX", "CTRA", "CVNA", "CVS", "CVX", "DAL", "DASH",
    "DDOG", "DIS", "DOW", "DVN", "EPD", "EQT", "FCX", "FSLR", "GE", "GILD",
    "GM", "GOOG", "GOOGL", "GSK", "HAL", "HOOD", "IBM", "INTC", "JPM", "KMI",
    "KO", "LRCX", "LUV", "LVS", "MDLZ", "META", "MMM", "MO", "MRK", "MRNA",
    "MRVL", "MS", "MSFT", "MSTR", "MU", "NEE", "NEM", "NFLX", "NKE", "NOW",
    "NVDA", "OKTA", "ORCL", "OXY", "PANW", "PDD", "PEP", "PG", "PLTR", "PYPL",
    "RBLX", "RCL", "RTX", "SBUX", "SCHW", "SHOP", "SLB", "TEVA", "TGT", "TSLA",
    "TSM", "UAL", "UBER", "UNH", "UPS", "V", "VST", "VZ", "WDC", "WFC",
    "WMT", "WYNN", "XOM", "Z",
]

BENCHMARK_SYMBOL = "SPY"

# Full universe including the benchmark, useful when pulling from price_cache
# in a single query.
FULL_PULL_LIST = WATCHLIST + [BENCHMARK_SYMBOL]

assert len(WATCHLIST) == 104, f"Expected 104 tickers, got {len(WATCHLIST)}"
