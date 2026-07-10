from oiapp.db import init_db, get_symbols
from oiapp.services.market import get_expirations, fetch_store_for
from oiapp.services.scheduler import run_scheduled_job


def main():
    init_db()
    syms = get_symbols()
    #print(f"Fetching data for:", syms )
    if not syms:
        syms = ["SPY"]

    for s in syms:
        exps = get_expirations(s)[:10]
        if exps:
            fetch_store_for(s, exps)
            print("Fetched:", s, len(exps), "expirations")
    run_scheduled_job(syms)
if __name__ == "__main__":
    main()
