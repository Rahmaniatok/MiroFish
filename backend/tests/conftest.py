import pytest


@pytest.fixture(autouse=True)
def _no_company_map_download(monkeypatch):
    """Locking a universe also builds its company map; never download prices in tests."""
    from app.news_pipeline import company_map
    monkeypatch.setattr(company_map, "fetch_close_history", lambda tickers, start, end, adjusted=True: {})
    company_map._PRICE_CACHE.clear()
