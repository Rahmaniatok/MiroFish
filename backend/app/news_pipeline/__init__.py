"""
News pipeline: Finnhub news -> original MiroFish (seed -> graph -> simulation
-> report) -> JSON -> consensus -> performance. One resumable run per
ticker_universe; see run_store.py for the on-disk layout.
"""
