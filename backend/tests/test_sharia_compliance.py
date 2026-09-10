"""
Phase 1g — tes screening kepatuhan syariah (AAOIFI SS-21).

Offline: `yf.Ticker` di-patch dengan `.info` palsu; `get_price_data` /
`get_fundamental_data` di-stub. Tidak ada jaringan.

Fokus:
  - _classify_sharia_sector_exclusion() mendiskriminasi dengan benar
    (Technology lolos, conventional bank kena, alkohol/tembakau/judi/pertahanan
    kena, minuman non-alkohol TIDAK kena)
  - debt_to_market_cap dihitung benar & berbeda dari debt_to_equity
  - is_point_in_time selalu False + warning
  - unavailable_checks selalu ada dan menyebut kriteria yang tak terhitung
  - get_stock_context() memuat sub-dict "sharia_compliance"
"""

import pytest

from app.data_layer import market_data
from app.data_layer.market_data import (
    _classify_sharia_sector_exclusion,
    fetch_sharia_compliance_data,
    get_stock_context,
)


class _FakeTicker:
    def __init__(self, info):
        self.info = info


def _patch_info(monkeypatch, info):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda t: _FakeTicker(info))


# ---------------------------------------------------------------------------
# business-activity screen (pure function)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sector,industry,expected_flag,expected_category", [
    ("Technology", "Consumer Electronics", False, None),
    ("Healthcare", "Drug Manufacturers - General", False, None),
    ("Consumer Defensive", "Beverages - Non-Alcoholic", False, None),   # KO — NOT alcohol
    ("Consumer Defensive", "Packaged Foods", False, None),
    ("Financial Services", "Banks - Diversified", True, "conventional_finance"),   # JPM
    ("Financial Services", "Banks - Regional", True, "conventional_finance"),
    ("Financial Services", "Insurance - Life", True, "conventional_finance"),
    ("Financial Services", "Capital Markets", True, "conventional_finance"),
    ("Financial Services", "Some New Fintech Thing", True, "conventional_finance"),  # sector net
    ("Consumer Defensive", "Beverages - Brewers", True, "alcohol"),
    ("Consumer Defensive", "Beverages - Wineries & Distilleries", True, "alcohol"),
    ("Consumer Defensive", "Tobacco", True, "tobacco"),
    ("Consumer Cyclical", "Gambling", True, "gambling"),
    ("Consumer Cyclical", "Resorts & Casinos", True, "gambling"),
    ("Industrials", "Aerospace & Defense", True, "defense_weapons"),
])
def test_classify_sector_exclusion(sector, industry, expected_flag, expected_category):
    result = _classify_sharia_sector_exclusion(sector, industry)
    assert result["flag"] is expected_flag
    assert result["category"] == expected_category
    if expected_flag:
        assert isinstance(result["reason"], str) and result["reason"]  # non-empty human string
    else:
        assert result["reason"] is None


def test_classify_industry_match_beats_sector_net():
    # specific industry match -> reason names the industry, not the sector
    r = _classify_sharia_sector_exclusion("Financial Services", "Banks - Diversified")
    assert "industry" in r["reason"].lower()


def test_classify_case_insensitive_and_whitespace():
    r = _classify_sharia_sector_exclusion("  financial services ", "  BANKS - DIVERSIFIED  ")
    assert r["flag"] is True and r["category"] == "conventional_finance"


def test_classify_missing_both():
    r = _classify_sharia_sector_exclusion(None, None)
    assert r == {"flag": False, "category": None, "reason": None}


# ---------------------------------------------------------------------------
# fetch_sharia_compliance_data — ratio math + shape
# ---------------------------------------------------------------------------
_AAPL_INFO = {
    "sector": "Technology", "industry": "Consumer Electronics",
    "totalDebt": 84_343_996_416, "marketCap": 4_602_128_760_832,
    "longName": "Apple Inc.", "_x": 1,
}
_JPM_INFO = {
    "sector": "Financial Services", "industry": "Banks - Diversified",
    "totalDebt": 1_343_306_989_568, "marketCap": 942_885_240_832,
    "longName": "JPMorgan Chase & Co.", "_x": 1,
}


def test_fetch_aapl_not_excluded_debt_ratio_small(monkeypatch):
    _patch_info(monkeypatch, _AAPL_INFO)
    sc = fetch_sharia_compliance_data("AAPL", as_of_date="2024-06-01")

    assert sc["success"] is True
    assert sc["sector_exclusion_flag"] is False
    assert sc["excluded_category"] is None
    assert sc["exclusion_reason"] is None
    # 84.34e9 / 4602.13e9 ≈ 0.0183
    assert sc["debt_to_market_cap"] == pytest.approx(0.018327, abs=1e-5)
    assert sc["passes_debt_screen"] is True
    assert sc["overall_compliant"] is True
    assert sc["is_point_in_time"] is False
    assert "not a point-in-time" in sc["warning"]
    assert "as_of_date=2024-06-01 is a label only" in sc["warning"]


def test_fetch_jpm_sector_excluded(monkeypatch):
    _patch_info(monkeypatch, _JPM_INFO)
    sc = fetch_sharia_compliance_data("JPM", as_of_date="2024-06-01")

    assert sc["sector_exclusion_flag"] is True
    assert sc["excluded_category"] == "conventional_finance"
    assert "bank" in sc["exclusion_reason"].lower() or "financ" in sc["exclusion_reason"].lower()
    # debt ratio ~1.42 -> also fails the 33% debt screen
    assert sc["debt_to_market_cap"] > 1.0
    assert sc["passes_debt_screen"] is False
    assert sc["overall_compliant"] is False


def test_debt_screen_threshold_is_33_percent(monkeypatch):
    _patch_info(monkeypatch, _AAPL_INFO)
    sc = fetch_sharia_compliance_data("AAPL")
    assert sc["debt_to_market_cap_threshold"] == 0.33


@pytest.mark.parametrize("ratio,expected_pass", [
    (0.2990, True),    # comfortably under
    (0.3010, True),    # >30% but <33%  -> BORDERLINE: fails old rule, passes corrected rule
    (0.3299, True),    # just under 33%
    (0.3300, False),   # exactly at 33% -> strict "<" fails
    (0.3400, False),   # over
])
def test_debt_screen_boundary_at_33(monkeypatch, ratio, expected_pass):
    # totalDebt / marketCap == ratio, non-excluded sector so overall reflects the screen
    info = dict(_AAPL_INFO, totalDebt=int(round(ratio * 1_000_000)), marketCap=1_000_000)
    _patch_info(monkeypatch, info)
    sc = fetch_sharia_compliance_data("BORDER")

    assert sc["debt_to_market_cap"] == pytest.approx(ratio, abs=1e-6)
    assert sc["passes_debt_screen"] is expected_pass
    assert sc["sector_exclusion_flag"] is False
    assert sc["overall_compliant"] is expected_pass   # not excluded => tracks the debt screen


def test_debt_to_market_cap_is_distinct_from_debt_to_equity(monkeypatch):
    # totalDebt / marketCap, NOT debtToEquity
    info = dict(_AAPL_INFO, totalDebt=100, marketCap=400, debtToEquity=78.4)
    _patch_info(monkeypatch, info)
    sc = fetch_sharia_compliance_data("AAPL")
    assert sc["debt_to_market_cap"] == 0.25          # 100/400
    assert sc["debt_to_market_cap"] != 0.784         # would be debt_to_equity


def test_null_total_debt_is_flagged_not_guessed(monkeypatch):
    _patch_info(monkeypatch, dict(_AAPL_INFO, totalDebt=None))
    sc = fetch_sharia_compliance_data("XYZ")
    assert sc["debt_to_market_cap"] is None
    assert sc["passes_debt_screen"] is None
    assert "debt_to_market_cap" in sc["unavailable_checks"]
    # business screen still works
    assert sc["sector_exclusion_flag"] is False


def test_unavailable_checks_always_present(monkeypatch):
    _patch_info(monkeypatch, _AAPL_INFO)
    sc = fetch_sharia_compliance_data("AAPL")
    for key in ("impure_income_ratio", "interest_bearing_investments_ratio",
                "islamic_institution_carveout"):
        assert key in sc["unavailable_checks"]
        assert sc["unavailable_checks"][key]


def test_invalid_ticker_returns_error_shape(monkeypatch):
    _patch_info(monkeypatch, {"_only_one_key": None})
    sc = fetch_sharia_compliance_data("ZZZZ")
    assert sc["success"] is False
    assert sc["error"]
    assert sc["sector_exclusion_flag"] is None
    assert sc["is_point_in_time"] is False
    assert sc["unavailable_checks"]          # still enumerated


# ---------------------------------------------------------------------------
# get_stock_context integration
# ---------------------------------------------------------------------------
def test_get_stock_context_includes_sharia_subdict(monkeypatch):
    monkeypatch.setattr(market_data, "get_price_data",
                        lambda t, as_of_date=None: {"ticker": t, "success": True})
    monkeypatch.setattr(market_data, "get_fundamental_data",
                        lambda t, as_of_date=None: {"ticker": t, "success": True,
                                                    "company_name": "Apple Inc.",
                                                    "debt_to_equity": 0.78})
    _patch_info(monkeypatch, _AAPL_INFO)

    ctx = get_stock_context("AAPL", as_of_date="2024-06-01")
    assert "sharia_compliance" in ctx
    sc = ctx["sharia_compliance"]
    assert sc["ticker"] == "AAPL"
    assert sc["sector_exclusion_flag"] is False
    # top-level success unchanged: still price AND fundamental only
    assert ctx["success"] is True
    assert sc["debt_to_market_cap"] != ctx["fundamental"]["debt_to_equity"]
