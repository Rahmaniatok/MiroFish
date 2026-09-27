"""
市场数据获取模块
使用 yfinance 获取股票的历史价格与基本面数据，作为投资分析引擎的最底层
数据来源。后续阶段（图谱构建、Agent 模拟、组合配置）都通过本模块读取数据。

fetch_price_data / fetch_fundamental_data 是"裸抓取"函数，每次调用都会
真实请求 yfinance。get_price_data / get_fundamental_data 是带缓存的包装
函数（见 cache.py），业务代码应优先调用这两个，只有缓存未命中时才会落到
fetch_* 上。get_stock_context 把价格 + 基本面合并成一份统一的上下文，供
后续阶段（图谱构建等）直接消费。

============================================================================
KETERBATASAN PENTING — DATA FUNDAMENTAL BUKAN POINT-IN-TIME
(wajib dibaca sebelum dipakai untuk backtest, khususnya Phase 8)
============================================================================
yfinance `.info` hanya menyediakan snapshot fundamental TERKINI (P/E, P/B,
market cap, dst pada saat fungsi dipanggil) — yfinance TIDAK punya API
fundamental historis bawaan. Akibatnya, saat `as_of_date` diisi:

  - Data HARGA (fetch_price_data / get_price_data) benar-benar difilter
    sampai as_of_date — dijamin tidak ada kebocoran data masa depan (lihat
    komentar di dalam fetch_price_data, dan uji validasi di blok __main__
    file ini).
  - Data FUNDAMENTAL (fetch_fundamental_data / get_fundamental_data) TIDAK
    bisa difilter seperti itu. Yang dikembalikan tetap nilai fundamental
    SEKARANG, hanya ditandai lewat field "is_point_in_time": False dan
    "warning" berisi penjelasannya. Kode yang memakai fundamental dari
    fungsi ini untuk backtest historis harus sadar bahwa datanya adalah
    data "dari masa depan" relatif terhadap as_of_date, dan berpotensi
    menyebabkan lookahead bias selama keterbatasan ini belum diperbaiki.
  - TODO: integrasikan sumber data fundamental historis yang sungguh-
    sungguh point-in-time (mis. SEC EDGAR XBRL, SimFin, atau provider
    berbayar lain) sebelum Phase 8 (backtest) dianggap valid untuk
    strategi yang berbasis fundamental.
============================================================================
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import yfinance as yf
from dateutil.relativedelta import relativedelta
from yfinance.exceptions import YFException

from ..utils.logger import get_logger
from .cache import get_cached, set_cache

logger = get_logger('mirofish.data_layer.market_data')

# Pemetaan string `period` ala-yfinance ke rentang waktu mundur (lookback),
# dipakai untuk mode as_of_date — lihat _resolve_lookback_start()
_PERIOD_TO_RELATIVEDELTA = {
    "1d": relativedelta(days=1),
    "5d": relativedelta(days=5),
    "1mo": relativedelta(months=1),
    "3mo": relativedelta(months=3),
    "6mo": relativedelta(months=6),
    "1y": relativedelta(years=1),
    "2y": relativedelta(years=2),
    "5y": relativedelta(years=5),
    "10y": relativedelta(years=10),
}

# yfinance `.info` fields that are known to be poorly covered or inconsistently
# scaled: we do NOT return them, so callers never misread a None as a business
# fact ("no growth" / "no dividend"). Each entry is surfaced in the returned
# `skipped_fields` dict as {field name -> reason it is skipped}.
#
# Phase 1f (2026-09-08): ROE / Debt-to-Equity / EPS Growth / Dividend Yield from
# the reference criteria are now populated (see the roe / debt_to_equity /
# eps_growth / dividend_yield keys returned by fetch_fundamental_data). Only PEG
# is still skipped:
#   - PEG methodology is inconsistent across data providers and industries, and
#     produces meaningless negative/outlier values when earnings are negative or
#     shrinking; yfinance's trailingPegRatio is still ~12% None on a broad
#     sample. Since pe_ratio and eps_growth are both returned now, callers can
#     compute PEG themselves with one consistent formula instead of importing
#     yfinance's opaque field.
#   - dividend_yield uses trailingAnnualDividendYield (always a plain fraction,
#     0.0 for non-payers) instead of the version-unstable dividendYield.
_UNRELIABLE_FUNDAMENTAL_FIELDS = {
    "trailingPegRatio": "PEG methodology is inconsistent across industries/providers and returns "
                        "meaningless negative values when earnings are negative or declining; "
                        "since Phase 1f, PEG is not returned - callers can derive it from "
                        "pe_ratio / eps_growth with a consistent formula",
    "pegRatio": "same as trailingPegRatio, and this field is deprecated in some yfinance versions",
    "dividendYield": "this field's unit is inconsistent across yfinance versions (e.g. 2.41 meaning "
                     "2.41% vs 0.0241); since Phase 1f, dividend_yield uses trailingAnnualDividendYield "
                     "instead (always a plain fraction)",
    "enterpriseToRevenue": "low coverage, and meaningless for some industries (e.g. financials)",
    "enterpriseToEbitda": "same as enterpriseToRevenue",
    "targetMeanPrice": "analyst target prices lag and their sample size is opaque; not suitable as a "
                       "quantitative input",
}

# Bumped whenever the *meaning* of a cached fundamental field changes (not just
# when a field is added). get_fundamental_data treats any cached row whose
# schema_version != this as a miss and refetches, so stale rows can't leak a
# differently-scaled value downstream.
#   1  -> Phase 1a shape (pe_ratio, pb_ratio, market_cap, sector, ...)
#   2  -> Phase 1f: added company_name / roe / debt_to_equity / eps_growth /
#         dividend_yield; debt_to_equity normalized from percent to a plain ratio
_FUNDAMENTAL_SCHEMA_VERSION = 2


# ============================================================================
# Phase 1g — SHARIA (ISLAMIC) COMPLIANCE SCREENING
# ref: AAOIFI Shari'ah Standard No. 21 ("Financial Paper — Shares and Bonds")
# ============================================================================
# WHAT THIS COVERS (and, just as importantly, WHAT IT DOES NOT)
# ----------------------------------------------------------------------------
# AAOIFI SS-21 share screening has, in practice, two kinds of test:
#
#   (A) Business-activity screen  -> is the company's PRIMARY line of business
#       impermissible? (conventional interest-based finance, alcohol, tobacco,
#       gambling, pork, adult entertainment, weapons, ...). We implement this
#       for the subset the task asked for — conventional financials, alcohol,
#       tobacco, gambling, defense/weapons — as an EXPLICIT, auditable mapping
#       from the yfinance sector/industry taxonomy (see _SHARIA_* dicts below).
#       No LLM, no keyword-guessing on free-text company descriptions.
#
#   (B) Financial-ratio screens  -> AAOIFI caps, roughly:
#         - interest-bearing debt / market cap            < 33%
#         - (cash + interest-bearing securities) / mcap   < 30%
#         - impure (interest / non-compliant) income      <  5% of total income
#       We can ONLY compute the first one here (debt_to_market_cap), because
#       yfinance exposes totalDebt + marketCap but NOT a breakdown of
#       interest-bearing investments or a line-item income statement. The rest
#       are returned in `unavailable_checks` as EXPLICITLY NOT COMPUTED — never
#       estimated or fabricated (same philosophy as the Phase 1c fundamental
#       limitations). A real deployment needs a specialist provider (IdealRatings,
#       Musaffa, Zoya, S&P/AAOIFI-certified feeds) for those.
#
# yfinance's totalDebt: interest-bearing short- + long-term debt — the correct
# basis for the AAOIFI debt screen. Coverage on the S&P 500 universe (Phase 1d)
# is good; it can be null on ADRs, small caps and non-standard filers, so
# debt_to_market_cap is returned as None (with a note) rather than guessed.
# It is a SPOT figure, whereas some boards use a trailing-12/24-month average
# market cap — a known, documented approximation, not a silent one.
#
# Islamic vs conventional banks: the sector/industry feed cannot tell a
# Shari'ah-compliant Islamic bank apart from a conventional one, so every
# "Banks"/"Insurance" industry is treated as excluded. Flagged in
# `unavailable_checks`.
# ----------------------------------------------------------------------------

_SHARIA_STANDARD = "AAOIFI Shari'ah Standard No. 21"

# AAOIFI debt screen threshold: interest-bearing debt / market cap must be < 33%
# (per AAOIFI Shari'ah Standard No. 21 as cited in the project reference material;
# note some other screening boards, e.g. AAOIFI-conservative readings, use 30%).
_SHARIA_DEBT_TO_MCAP_THRESHOLD = 0.33

# category key -> human-readable label (used to build exclusion_reason)
_SHARIA_CATEGORY_LABELS = {
    "conventional_finance": "conventional (interest-based) financials — banking / insurance / lending",
    "alcohol": "alcohol production or distribution",
    "tobacco": "tobacco",
    "gambling": "gambling / casinos",
    "defense_weapons": "defense / weapons manufacturing",
}

# EXPLICIT mapping: yfinance `industry` string (lower-cased) -> prohibited category.
# yfinance uses Yahoo's taxonomy, not raw GICS — the financial `sector` is
# "Financial Services" and industries look like "Banks - Diversified". Matching is
# exact (case-insensitive) on the structured `industry` field.
_SHARIA_PROHIBITED_INDUSTRIES = {
    # --- conventional financials -------------------------------------------
    "banks - diversified": "conventional_finance",
    "banks - regional": "conventional_finance",
    "banks": "conventional_finance",
    "mortgage finance": "conventional_finance",
    "financial - mortgages": "conventional_finance",
    "credit services": "conventional_finance",
    "financial - credit services": "conventional_finance",
    "capital markets": "conventional_finance",
    "financial - capital markets": "conventional_finance",
    "financial data & stock exchanges": "conventional_finance",
    "financial - data & stock exchanges": "conventional_finance",
    "insurance - diversified": "conventional_finance",
    "insurance - life": "conventional_finance",
    "insurance - property & casualty": "conventional_finance",
    "insurance - reinsurance": "conventional_finance",
    "insurance - specialty": "conventional_finance",
    "insurance brokers": "conventional_finance",
    "financial conglomerates": "conventional_finance",
    "asset management": "conventional_finance",
    "shell companies": "conventional_finance",
    # --- alcohol ----------------------------------------------------------
    "beverages - brewers": "alcohol",
    "beverages - wineries & distilleries": "alcohol",
    # --- tobacco --------------------------------------------------------
    "tobacco": "tobacco",
    # --- gambling -------------------------------------------------------
    "gambling": "gambling",
    "resorts & casinos": "gambling",
    # --- defense / weapons --------------------------------------------
    "aerospace & defense": "defense_weapons",
}

# Broad safety net: if yfinance puts a name in this `sector` but its specific
# `industry` string isn't in the map above, still exclude it (conservative — an
# over-exclusion in a compliance HARD filter is the safe direction). Reason will
# name the sector rather than the industry.
_SHARIA_PROHIBITED_SECTORS = {
    "financial services": "conventional_finance",
}

# AAOIFI SS-21 criteria we CANNOT compute from yfinance — returned verbatim in
# the `unavailable_checks` field. Not estimated, not fabricated.
_SHARIA_UNAVAILABLE_CHECKS = {
    "impure_income_ratio": "Interest income + other non-compliant revenue as a share of total "
                           "income (AAOIFI cap: < 5%). Needs a line-item income statement / "
                           "revenue-source breakdown that yfinance does not provide.",
    "interest_bearing_investments_ratio": "(Cash + interest-bearing securities) / market cap "
                                          "(AAOIFI cap: < 30%). yfinance has no split of "
                                          "interest-bearing vs non-interest-bearing investments.",
    "illiquid_asset_ratio": "Tangible/illiquid assets as a share of total assets (some boards "
                            "require >= 30% for share tradability). Not derivable here.",
    "dividend_purification_amount": "Per-share amount to donate to purify impure income. "
                                    "Requires impure_income_ratio, which is unavailable.",
    "subsidiary_lookthrough": "AAOIFI requires assessing non-compliant activities of "
                              "subsidiaries/associates; structured feeds do not expose this.",
    "islamic_institution_carveout": "The sector/industry feed cannot distinguish a "
                                    "Shari'ah-compliant Islamic bank/insurer (takaful) from a "
                                    "conventional one — all 'Banks' / 'Insurance' industries are "
                                    "flagged as excluded regardless.",
}

# Bumped when the meaning/shape of a cached sharia row changes (cache-busting,
# same mechanism as _FUNDAMENTAL_SCHEMA_VERSION).
#   1 -> Phase 1g initial shape
#   2 -> debt screen threshold corrected 0.30 -> 0.33 (changes passes_debt_screen /
#        overall_compliant / debt_to_market_cap_threshold on cached rows)
_SHARIA_SCHEMA_VERSION = 2


# Dipakai sebagai pengganti "tanpa batas bawah" untuk period="max" (lihat
# _resolve_lookback_start) — lebih tua dari IPO saham manapun yang realistis
_EARLIEST_POSSIBLE_DATE = date(1900, 1, 1)

# Phase 1e: indikator lambat (SMA200, warm-up MACD) butuh histori panjang.
# Fetch price SELALU diperlebar minimal sekian tahun ke belakang untuk warm-up,
# lalu ohlcv + stats "52 minggu" dipotong balik ke `period` yang diminta
# (lihat _trim_history_to_period). Perlebaran ini HANYA ke masa lalu — tidak
# pernah melewati as_of_date (lihat komentar besar di fetch_price_data).
_INDICATOR_LOOKBACK_YEARS = 2
_MIN_INDICATOR_PERIOD = "2y"
# urut dari terpendek ke terpanjang, dipakai _widen_period_for_indicators
_PERIOD_LENGTH_ORDER = ("1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max")


def _resolve_lookback_start(as_of: date, period: str) -> date:
    """
    Menentukan tanggal mulai (start) berdasarkan `period`, khusus dipakai
    saat mode as_of_date aktif.

    yfinance TIDAK bisa menerima `period` bersamaan dengan `start`/`end` —
    begitu `start`/`end` diisi, `period` diabaikan sepenuhnya oleh library
    (lihat signature asli di yfinance.scrapers.history.PriceHistory.history:
    period='1mo if start & end None'). Karena mode as_of_date *harus* pakai
    start/end (supaya `end` bisa dipatok tepat setelah as_of_date, lihat
    fetch_price_data), `period` kita terjemahkan sendiri jadi tanggal mulai
    di sini, supaya jendela lookback-nya tetap konsisten dengan mode live.

    CATATAN(diverifikasi manual): start=None BUKAN berarti "tanpa batas bawah"
    bagi yfinance ketika `end` diisi — yfinance.Ticker.history(start=None,
    end="2024-06-01") ternyata cuma mengembalikan ~1 bulan data, bukan seluruh
    histori sejak IPO. Karena itu period="max" di sini memakai tanggal yang
    sangat lampau (_EARLIEST_POSSIBLE_DATE) sebagai start, bukan None.
    """
    if period == "ytd":
        return date(as_of.year, 1, 1)
    if period == "max":
        return _EARLIEST_POSSIBLE_DATE
    delta = _PERIOD_TO_RELATIVEDELTA.get(period)
    if delta is None:
        logger.warning(f"period '{period}' 未知，as_of_date 模式下回退为 '1y'")
        delta = _PERIOD_TO_RELATIVEDELTA["1y"]
    return as_of - delta


def _widen_period_for_indicators(period: str) -> str:
    """
    Mode live: pastikan fetch cukup panjang untuk warm-up SMA200/MACD (minimal
    _MIN_INDICATOR_PERIOD). period yang sudah >= 2y dibiarkan apa adanya; yang
    lebih pendek atau tidak dikenal dinaikkan ke '2y'. ohlcv yang dikembalikan
    tetap dipotong ke `period` asli lewat _trim_history_to_period().
    """
    try:
        if _PERIOD_LENGTH_ORDER.index(period) >= _PERIOD_LENGTH_ORDER.index(_MIN_INDICATOR_PERIOD):
            return period
    except ValueError:
        pass
    return _MIN_INDICATOR_PERIOD


def _trim_history_to_period(history, period: str, ref_end_date: date):
    """
    Potong `history` (yang sengaja di-fetch lebih panjang demi warm-up
    indikator) kembali ke jendela `period` yang diminta, berakhir di
    ref_end_date.

    Ini HANYA memengaruhi ohlcv + stats "52 minggu" yang dikembalikan.
    technical_indicators dihitung SEBELUM pemotongan ini, di atas seri penuh —
    jadi memperlebar fetch demi warm-up TIDAK diam-diam mengubah "52w_high"
    menjadi "high beberapa tahun".
    """
    window_start = _resolve_lookback_start(ref_end_date, period)
    return history[history.index.date >= window_start]


def fetch_price_data(ticker: str, period: str = "1y", as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    获取股票历史价格数据(OHLCV)及基础衍生统计指标

    Args:
        ticker: 股票代码，如 "AAPL"
        period: yfinance 支持的周期字符串，如 "1mo"/"3mo"/"1y"/"5y"/"max"/"ytd"。
            无论 live 还是 as_of_date 模式，都是作为"向前回溯多久"的窗口长度，
            决定返回的 ohlcv 和 stats("52周") 的范围。
            注意(Phase 1e): technical_indicators 需要更长的历史(SMA200 等)，
            所以内部实际抓取的区间会被自动放宽到至少约 2 年，再把 ohlcv/stats
            裁剪回 period；technical_indicators 用的是未裁剪的完整序列。
        as_of_date: None（默认）= mode live，行为跟以前一样，抓取截至今天的数据。
            Diisi string ISO "YYYY-MM-DD" = mode historis: HANYA data s.d.
            (dan termasuk) tanggal tersebut yang diambil dari yfinance, dan
            SEMUA statistik turunan (52w high/low, %perubahan 1bln/3bln/1thn)
            dihitung ulang HANYA dari data itu — tidak boleh ada satu baris
            pun bertanggal setelah as_of_date yang ikut terpakai (ini yang
            mencegah lookahead leakage saat data ini dipakai untuk
            backtest/simulasi historis).

    Returns:
        成功: {
            "ticker": str, "success": True, "error": None, "period": str,
            "as_of_date": str | None,
            "ohlcv": List[dict]（按日期升序，每条含 date/open/high/low/close/volume）,
            "latest_close": float, "latest_date": str,
            "stats": {
                "52w_high": float, "52w_low": float,
                "change_1mo_pct": float | None,
                "change_3mo_pct": float | None,
                "change_1y_pct": float | None,
            },
            "technical_indicators": {   # Phase 1e — semua field bisa None kalau data kurang
                "price_vs_sma50": float | None,   # (close - SMA50) / SMA50 * 100
                "price_vs_sma200": float | None,  # (close - SMA200) / SMA200 * 100
                "rsi_14": float | None,           # 0..100 (Wilder)
                "macd_signal": {"macd": float|None, "signal": float|None, "histogram": float|None},
                "bollinger_position": float | None,  # %B: 0=band bawah, 1=band atas
                "volume_vs_avg": float | None,       # volume terakhir / rata-rata 20 hari sebelumnya
                "change_1d_pct": float | None,       # perubahan harga 1 hari (%)
            }
        }
        失败: {"ticker": str, "success": False, "error": str, "period": None,
               "as_of_date": str | None, "ohlcv": None, "latest_close": None,
               "latest_date": None, "stats": None, "technical_indicators": None}
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _price_error(ticker, "股票代码不能为空", as_of_date)

    as_of: Optional[date] = None
    if as_of_date is not None:
        try:
            as_of = date.fromisoformat(as_of_date)
        except ValueError:
            return _price_error(
                ticker, f"as_of_date '{as_of_date}' bukan format tanggal ISO yang valid (YYYY-MM-DD)", as_of_date
            )

    try:
        if as_of is None:
            # Perlebar fetch supaya indikator lambat (SMA200, warm-up MACD)
            # punya cukup histori; ohlcv dipotong balik ke `period` di bawah.
            history = yf.Ticker(ticker).history(period=_widen_period_for_indicators(period))
        else:
            # ================= LOOKBACK vs. LEAKAGE — WAJIB BACA =================
            # Ada DUA hal berbeda yang sedang diseimbangkan di sini, gampang
            # tertukar dan menghasilkan bug halus:
            #
            #   1. LOOKBACK (butuh LEBIH BANYAK data masa lalu): indikator seperti
            #      SMA200 perlu ~200 hari dagang SEBELUM as_of_date. Karena itu
            #      awal jendela fetch mundur sampai _INDICATOR_LOOKBACK_YEARS
            #      tahun SEBELUM as_of_date, BUKAN mulai dari as_of_date. Kalau
            #      fetch dimulai tepat di as_of_date, SMA200 diam-diam jadi
            #      None / sampah.
            #
            #   2. LEAKAGE (TIDAK boleh ada data masa depan): tidak boleh ada
            #      SATU baris pun bertanggal SETELAH as_of_date yang ikut masuk
            #      ke perhitungan apa pun (ohlcv, stats, MAUPUN indikator).
            #      `end` dipatok di as_of_date + 1 hari, DAN ada filter eksplisit
            #      di bawah sebagai jaring pengaman.
            #
            # Ringkas: jendela HANYA dilebarkan ke MASA LALU; ujung kanannya
            # tidak pernah melewati as_of_date.
            # ===================================================================
            lookback_start = _resolve_lookback_start(as_of, period)
            indicator_start = as_of - relativedelta(years=_INDICATOR_LOOKBACK_YEARS)
            start = min(lookback_start, indicator_start)
            # CATATAN(diverifikasi manual): parameter `end` di yfinance bersifat
            # EKSKLUSIF — history(end="2024-05-31") berhenti di baris 2024-05-30,
            # baru history(end="2024-06-01") menyertakan baris 2024-05-31. Karena
            # itu `end` di sini HARUS as_of_date + 1 hari supaya as_of_date sendiri
            # ikut termasuk dalam hasil.
            end = as_of + timedelta(days=1)
            history = yf.Ticker(ticker).history(start=start, end=end)
    except YFException as e:
        logger.warning(f"获取 {ticker} 历史价格失败(yfinance异常): {e}")
        return _price_error(ticker, f"yfinance请求失败: {e}", as_of_date)
    except Exception as e:
        logger.warning(f"获取 {ticker} 历史价格失败: {e}")
        return _price_error(ticker, f"请求异常: {e}", as_of_date)

    if history is None or history.empty:
        logger.info(f"股票代码 {ticker} 未返回任何历史数据，可能是无效代码")
        return _price_error(ticker, f"未找到股票代码 '{ticker}' 的历史数据，可能是无效代码或已退市", as_of_date)

    if as_of is not None:
        # LEAKAGE GUARD (defense in depth): walaupun `end` di atas sudah dihitung
        # supaya tidak melewati as_of_date, di sini kita filter eksplisit sekali
        # lagi berdasarkan tanggal tiap baris. SEMUA perhitungan setelah baris
        # ini — ohlcv, stats, DAN technical_indicators — hanya boleh melihat
        # baris bertanggal <= as_of_date. Jangan menghitung indikator apa pun
        # dari `history` sebelum filter ini dijalankan.
        history = history[history.index.date <= as_of]
        if history.empty:
            return _price_error(
                ticker,
                f"股票代码 '{ticker}' 在 as_of_date={as_of_date} 或之前没有历史数据",
                as_of_date,
            )

    # --- Technical indicators (Phase 1e) ---
    # Dihitung dari SELURUH `history` yang (sudah difilter leak-safe di atas) —
    # justru butuh bagian yang lebih panjang dari `period` untuk warm-up.
    indicator_closes = [float(v) for v in history["Close"].tolist() if v == v]
    indicator_volumes = [float(v) for v in history["Volume"].tolist() if v == v]
    technical_indicators = _compute_technical_indicators(indicator_closes, indicator_volumes)

    # --- ohlcv + stats "52 minggu": HANYA jendela `period` yang diminta ---
    # (technical_indicators sudah dihitung di atas dari seri penuh, jadi
    # pemotongan ini tidak mempengaruhinya.)
    ref_end_date = as_of if as_of is not None else history.index[-1].date()
    window = _trim_history_to_period(history, period, ref_end_date)

    ohlcv: List[Dict[str, Any]] = []
    for row_date, row in window.iterrows():
        volume = row.get("Volume")
        ohlcv.append({
            "date": row_date.strftime("%Y-%m-%d"),
            "open": _round_or_none(row.get("Open")),
            "high": _round_or_none(row.get("High")),
            "low": _round_or_none(row.get("Low")),
            "close": _round_or_none(row.get("Close")),
            "volume": int(volume) if volume == volume else None,  # volume != volume <=> NaN
        })

    closes = window["Close"]
    # 注意: 当可用数据只有约252个交易日时（例如 period="1y"），不足以覆盖
    # "252个交易日前"这一比较点，此时 change_1y_pct 会是 None。如需稳定获取
    # 1年涨跌幅，调用方可传入更长的 period（如 "2y"）。
    stats = {
        "52w_high": _round_or_none(closes.max()),
        "52w_low": _round_or_none(closes.min()),
        "change_1mo_pct": _pct_change_over_trading_days(closes, 21),
        "change_3mo_pct": _pct_change_over_trading_days(closes, 63),
        "change_1y_pct": _pct_change_over_trading_days(closes, 252),
    }

    return {
        "ticker": ticker,
        "success": True,
        "error": None,
        "period": period,
        "as_of_date": as_of_date,
        "ohlcv": ohlcv,
        "latest_close": _round_or_none(closes.iloc[-1]),
        "latest_date": window.index[-1].strftime("%Y-%m-%d"),
        "stats": stats,
        "technical_indicators": technical_indicators,
    }


def fetch_fundamental_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    获取股票基本面数据（估值、盈利能力等）

    只返回 yfinance `.info` 中覆盖率较高、口径相对稳定的字段；已知经常为
    None 或口径不一致的字段（见 _UNRELIABLE_FUNDAMENTAL_FIELDS）不返回，
    而是列在 skipped_fields 中（字段名 -> 跳过原因），方便调用方了解数据边界。

    PENTING — keterbatasan as_of_date (lihat juga catatan besar di docstring
    modul ini): yfinance `.info` TIDAK punya API fundamental historis. Jadi
    ketika `as_of_date` diisi, fungsi ini TETAP memanggil `.info` dan
    mengembalikan nilai fundamental SEKARANG (bukan snapshot pada
    as_of_date) — hanya ditandai `is_point_in_time=False` beserta pesan di
    `warning`, supaya pemanggil (terutama modul backtest di Phase 8) tidak
    diam-diam menganggap data ini akurat secara historis.
    TODO(masa depan): ganti dengan sumber data fundamental historis yang
    sungguh-sungguh point-in-time sebelum dipakai untuk backtest serius.

    Args:
        ticker: 股票代码，如 "AAPL"
        as_of_date: None = data fundamental "saat ini" (is_point_in_time=True).
            Diisi string ISO "YYYY-MM-DD" = tetap mengambil data `.info`
            terkini (lihat keterbatasan di atas), namun hasilnya ditandai
            is_point_in_time=False beserta warning-nya.

    Returns:
        success: {
            "ticker": str, "success": True, "error": None,
            "as_of_date": str | None,
            "schema_version": int,        # _FUNDAMENTAL_SCHEMA_VERSION; used for cache busting
            "is_point_in_time": bool, "warning": str | None,
            "company_name": str | None,   # yfinance longName (falls back to shortName)
            "pe_ratio": float | None, "pb_ratio": float | None,
            "market_cap": int | None, "sector": str | None, "industry": str | None,
            "revenue_growth_yoy": float | None, "profit_margin": float | None,
            # Phase 1f reference criteria:
            "roe": float | None,             # returnOnEquity, plain fraction (0.15 = 15%)
            "debt_to_equity": float | None,  # plain ratio (0.78 = 0.78x); yfinance's
                                             #   debtToEquity is a percent number and is
                                             #   divided by 100 here (see _percent_to_ratio)
            "eps_growth": float | None,      # earningsGrowth, YoY, plain fraction
            "dividend_yield": float | None,  # trailingAnnualDividendYield, plain fraction;
                                             #   0.0 (not None) for non-payers
            "skipped_fields": {field_name: reason_it_is_skipped},
        }
        failure: {"ticker": str, "success": False, "error": str, ...all other fields None,
                  "skipped_fields": {...}}

    In as_of_date mode every fundamental field above (including the 5 added in
    Phase 1f) still carries is_point_in_time=False and the warning - like
    pe_ratio and the other older fields, they are the CURRENT `.info` snapshot,
    not the value as of as_of_date.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _fundamental_error(ticker, "股票代码不能为空", as_of_date)

    try:
        info = yf.Ticker(ticker).info
    except YFException as e:
        logger.warning(f"获取 {ticker} 基本面数据失败(yfinance异常): {e}")
        return _fundamental_error(ticker, f"yfinance请求失败: {e}", as_of_date)
    except Exception as e:
        logger.warning(f"获取 {ticker} 基本面数据失败: {e}")
        return _fundamental_error(ticker, f"请求异常: {e}", as_of_date)

    # 无效股票代码时 yfinance 返回近乎空的 info（通常只剩1个全为 None 的字段）
    if not info or len(info) <= 1:
        logger.info(f"股票代码 {ticker} 未返回任何基本面数据，可能是无效代码")
        return _fundamental_error(ticker, f"未找到股票代码 '{ticker}' 的基本面数据，可能是无效代码或已退市", as_of_date)

    # trailingPE 覆盖率更高，缺失时退化到 forwardPE
    pe_ratio = info.get("trailingPE")
    if pe_ratio is None:
        pe_ratio = info.get("forwardPE")

    is_point_in_time = as_of_date is None
    warning = None
    if not is_point_in_time:
        warning = (
            f"Data fundamental ini adalah nilai TERKINI (saat fungsi dipanggil), "
            f"BUKAN snapshot historis pada as_of_date={as_of_date} — yfinance belum "
            f"punya sumber data fundamental historis yang terintegrasi di sini."
        )

    return {
        "ticker": ticker,
        "success": True,
        "error": None,
        "as_of_date": as_of_date,
        "schema_version": _FUNDAMENTAL_SCHEMA_VERSION,
        "is_point_in_time": is_point_in_time,
        "warning": warning,
        # longName 覆盖率极高（宽口径样本 0/40 缺失），只有无效代码才为 None；
        # 直接取自 .info，不依赖 Phase 1d 的 S&P 500 名单，任意 ticker 都能用。
        "company_name": info.get("longName") or info.get("shortName"),
        "pe_ratio": pe_ratio,
        "pb_ratio": info.get("priceToBook"),
        "market_cap": info.get("marketCap"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "revenue_growth_yoy": info.get("revenueGrowth"),
        "profit_margin": info.get("profitMargins"),
        # --- Phase 1f: same access pattern as the older fields above (plain
        #     info.get), and subject to the same is_point_in_time / warning ---
        "roe": info.get("returnOnEquity"),
        # yfinance reports debtToEquity as a PERCENTAGE number, not a ratio
        # (verified against balance sheets: MSFT debtToEquity 29.118 == totalDebt
        # / equity 0.2912; AAPL 78.445 -> 0.78). Normalize to a plain ratio here
        # so downstream code reads 0.78x, not 78x — matching how dividend_yield
        # is stored as a fraction (0.0033, not 0.33).
        "debt_to_equity": _percent_to_ratio(info.get("debtToEquity")),
        "eps_growth": info.get("earningsGrowth"),
        # dividendYield's unit is unstable (see _UNRELIABLE_FUNDAMENTAL_FIELDS);
        # trailingAnnualDividendYield is always a plain fraction, 0.0 for non-payers.
        "dividend_yield": info.get("trailingAnnualDividendYield"),
        "skipped_fields": dict(_UNRELIABLE_FUNDAMENTAL_FIELDS),
    }


def get_price_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    带缓存的价格数据获取(推荐业务代码使用此函数而不是 fetch_price_data)

    先查 cache.py 中的本地缓存，命中且未过期则直接返回；未命中才会真正
    调用 fetch_price_data 请求 yfinance，并把成功的结果写回缓存。

    Args:
        ticker: 股票代码
        as_of_date: None 表示查询"实时"数据(缓存有 TTL)；传入 ISO 日期
            字符串表示查询/缓存该日期对应的历史快照(缓存永不过期)。自
            Phase 1c 起，as_of_date 会真正透传给 fetch_price_data 用于
            过滤数据范围，不再只是缓存键。

    自 Phase 1e 起，fetch_price_data 的返回值多了一个 "technical_indicators"
    字段。本函数不需要任何改动即可透传它——缓存的是 fetch_price_data 的整个
    dict(同一个 (ticker, as_of_date) 的 JSON blob)，technical_indicators 天然
    包含在内，缓存表结构无需变更。
    """
    ticker = (ticker or "").strip().upper()

    cached = get_cached(ticker, "price", as_of_date)
    if cached is not None and "technical_indicators" in cached:
        logger.info(f"Cache hit for {ticker}/price" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached
    if cached is not None:
        # 命中了 Phase 1e 之前写入的旧缓存(没有 technical_indicators 字段)：
        # 当作未命中，重新抓取并覆盖，避免下游拿到缺字段的结果。
        logger.info(f"Cache hit but stale schema (no technical_indicators), refetching: {ticker}/price"
                    + (f"@{as_of_date}" if as_of_date else " (live)"))

    logger.info(f"Cache miss, fetching from yfinance: {ticker}/price" + (f"@{as_of_date}" if as_of_date else " (live)"))
    result = fetch_price_data(ticker, as_of_date=as_of_date)

    # 只缓存成功结果：抓取失败(无效代码/限流等)可能是暂时性的，不应该让调用方
    # 在 TTL 窗口内(或历史快照场景下永久)反复拿到同一条错误
    if result.get("success"):
        set_cache(ticker, "price", result, as_of_date)

    return result


def get_fundamental_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    带缓存的基本面数据获取(推荐业务代码使用此函数而不是 fetch_fundamental_data)

    缓存策略与 get_price_data 一致，见其文档说明。

    PERHATIAN: seperti dijelaskan di fetch_fundamental_data, mengisi
    as_of_date DI SINI tidak membuat datanya jadi point-in-time — yang
    di-cache di bawah key historis (as_of_date, tidak pernah expired) tetap
    nilai fundamental SAAT fungsi ini pertama kali dipanggil untuk key
    tersebut, ditandai is_point_in_time=False. Lihat field "warning" pada
    hasilnya.
    """
    ticker = (ticker or "").strip().upper()

    cached = get_cached(ticker, "fundamental", as_of_date)
    if cached is not None and cached.get("schema_version") == _FUNDAMENTAL_SCHEMA_VERSION:
        logger.info(f"Cache hit for {ticker}/fundamental" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached
    if cached is not None:
        # Cached row predates the current fundamental schema (missing fields, or
        # a field whose meaning/scale has since changed - e.g. pre-1f rows, or
        # 1f rows written before debt_to_equity was normalized). Treat it as a
        # miss and refetch/overwrite so downstream never sees a stale-shaped row.
        # (Same approach get_price_data uses for pre-1e cached rows.)
        logger.info(
            f"Cache hit but stale schema "
            f"(cached v{cached.get('schema_version')!r} != v{_FUNDAMENTAL_SCHEMA_VERSION}), "
            f"refetching: {ticker}/fundamental" + (f"@{as_of_date}" if as_of_date else " (live)")
        )

    logger.info(f"Cache miss, fetching from yfinance: {ticker}/fundamental" + (f"@{as_of_date}" if as_of_date else " (live)"))
    result = fetch_fundamental_data(ticker, as_of_date=as_of_date)

    if result.get("success"):
        set_cache(ticker, "fundamental", result, as_of_date)

    return result


# ============================================================================
# Phase 1g — Sharia compliance screening (AAOIFI SS-21). See the big block near
# the top of this module for scope + limitations.
# ============================================================================
def _classify_sharia_sector_exclusion(
    sector: Optional[str], industry: Optional[str]
) -> Dict[str, Optional[str]]:
    """
    Business-activity screen: map the (already-structured) yfinance
    sector/industry onto the prohibited-category list. Deterministic, explicit,
    no LLM / no free-text keyword guessing.

    Precedence: specific `industry` match first (more precise reason), then the
    broad `sector` safety net.

    Returns {"flag": bool, "reason": str | None, "category": str | None}.
    """
    industry_norm = (industry or "").strip().lower()
    sector_norm = (sector or "").strip().lower()

    category = _SHARIA_PROHIBITED_INDUSTRIES.get(industry_norm)
    if category is not None:
        label = _SHARIA_CATEGORY_LABELS[category]
        return {
            "flag": True,
            "category": category,
            "reason": f"{label} (yfinance industry: {industry!r})",
        }

    category = _SHARIA_PROHIBITED_SECTORS.get(sector_norm)
    if category is not None:
        label = _SHARIA_CATEGORY_LABELS[category]
        return {
            "flag": True,
            "category": category,
            "reason": (
                f"{label} (yfinance sector: {sector!r}; specific industry "
                f"{industry!r} not individually listed — sector-level exclusion)"
            ),
        }

    return {"flag": False, "category": None, "reason": None}


def _sharia_error(ticker: str, error: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "success": False,
        "error": error,
        "as_of_date": as_of_date,
        "schema_version": _SHARIA_SCHEMA_VERSION,
        "standard": _SHARIA_STANDARD,
        "is_point_in_time": False,
        "warning": None,
        "total_debt": None,
        "market_cap": None,
        "debt_to_market_cap": None,
        "debt_to_market_cap_threshold": _SHARIA_DEBT_TO_MCAP_THRESHOLD,
        "passes_debt_screen": None,
        "sector": None,
        "industry": None,
        "sector_exclusion_flag": None,
        "excluded_category": None,
        "exclusion_reason": None,
        "overall_compliant": None,
        "unavailable_checks": dict(_SHARIA_UNAVAILABLE_CHECKS),
    }


def fetch_sharia_compliance_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute the AAOIFI SS-21 share-screening signals we can derive from
    yfinance: the interest-bearing-debt / market-cap ratio, and the
    business-activity (sector/industry) exclusion flag.

    Like fetch_fundamental_data, this is NOT point-in-time — it reads the
    current yfinance `.info` snapshot (totalDebt, marketCap, sector, industry).
    When `as_of_date` is set, the result still carries is_point_in_time=False
    and a `warning`.

    Args:
        ticker: 股票代码, e.g. "AAPL"
        as_of_date: None = live; ISO "YYYY-MM-DD" = still the current snapshot,
            marked is_point_in_time=False.

    Returns:
        success: {
            "ticker": str, "success": True, "error": None,
            "as_of_date": str | None,
            "schema_version": int,           # _SHARIA_SCHEMA_VERSION, cache-busting
            "standard": str,                 # "AAOIFI Shari'ah Standard No. 21"
            "is_point_in_time": False, "warning": str | None,

            "total_debt": int | None,        # yfinance totalDebt (interest-bearing)
            "market_cap": int | None,
            "debt_to_market_cap": float | None,   # total_debt / market_cap; DISTINCT
                                                  #   from fundamental.debt_to_equity
            "debt_to_market_cap_threshold": 0.33, # AAOIFI cap (SS-21, per project ref)
            "passes_debt_screen": bool | None,    # ratio < threshold; None if ratio None

            "sector": str | None, "industry": str | None,
            "sector_exclusion_flag": bool,   # True => prohibited primary business
            "excluded_category": str | None, # machine key: conventional_finance / alcohol / ...
            "exclusion_reason": str | None,  # human explanation, or None if not excluded

            "overall_compliant": bool | None,# NOT excluded AND passes_debt_screen;
                                             #   None if the debt screen is indeterminate.
                                             #   PARTIAL — see unavailable_checks.
            "unavailable_checks": {check: reason_it_cannot_be_computed},
        }
        failure: {"ticker": str, "success": False, "error": str, ...others None/def}
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _sharia_error(ticker, "股票代码不能为空", as_of_date)

    try:
        info = yf.Ticker(ticker).info
    except YFException as e:
        logger.warning(f"获取 {ticker} Sharia 数据失败(yfinance异常): {e}")
        return _sharia_error(ticker, f"yfinance请求失败: {e}", as_of_date)
    except Exception as e:
        logger.warning(f"获取 {ticker} Sharia 数据失败: {e}")
        return _sharia_error(ticker, f"请求异常: {e}", as_of_date)

    if not info or len(info) <= 1:
        logger.info(f"股票代码 {ticker} 未返回任何数据，可能是无效代码 (Sharia)")
        return _sharia_error(
            ticker, f"未找到股票代码 '{ticker}' 的数据，可能是无效代码或已退市", as_of_date
        )

    sector = info.get("sector")
    industry = info.get("industry")
    total_debt = info.get("totalDebt")
    market_cap = info.get("marketCap")

    # --- financial-ratio screen: debt / market cap ---
    debt_to_market_cap: Optional[float] = None
    if (
        total_debt is not None and total_debt == total_debt          # not NaN
        and market_cap is not None and market_cap == market_cap
        and market_cap > 0
    ):
        debt_to_market_cap = round(float(total_debt) / float(market_cap), 6)
    passes_debt_screen: Optional[bool] = (
        None if debt_to_market_cap is None
        else debt_to_market_cap < _SHARIA_DEBT_TO_MCAP_THRESHOLD
    )

    # --- business-activity screen: sector/industry exclusion ---
    exclusion = _classify_sharia_sector_exclusion(sector, industry)

    # --- overall (PARTIAL — only the checks we can actually run) ---
    if exclusion["flag"]:
        overall_compliant: Optional[bool] = False
    elif passes_debt_screen is None:
        overall_compliant = None            # can't confirm without the debt ratio
    else:
        overall_compliant = bool(passes_debt_screen)

    warning = (
        "Sharia screening signals are derived from the CURRENT yfinance snapshot "
        "(totalDebt / marketCap / sector), not a point-in-time record"
    )
    if as_of_date is not None:
        warning += f"; as_of_date={as_of_date} is a label only, the data is today's"
    warning += ". PARTIAL: only the debt and business-activity screens are run — "
    warning += "see unavailable_checks for AAOIFI criteria not computed."

    unavailable = dict(_SHARIA_UNAVAILABLE_CHECKS)
    if debt_to_market_cap is None:
        unavailable["debt_to_market_cap"] = (
            f"yfinance returned totalDebt={total_debt!r} / marketCap={market_cap!r}; "
            f"the debt screen could not be computed for this ticker."
        )
    if not sector and not industry:
        unavailable["business_activity_screen"] = (
            "yfinance returned no sector/industry for this ticker; the exclusion "
            "flag defaults to False but the primary business could not be verified."
        )

    return {
        "ticker": ticker,
        "success": True,
        "error": None,
        "as_of_date": as_of_date,
        "schema_version": _SHARIA_SCHEMA_VERSION,
        "standard": _SHARIA_STANDARD,
        "is_point_in_time": False,
        "warning": warning,
        "total_debt": total_debt if (total_debt == total_debt) else None,
        "market_cap": market_cap if (market_cap == market_cap) else None,
        "debt_to_market_cap": debt_to_market_cap,
        "debt_to_market_cap_threshold": _SHARIA_DEBT_TO_MCAP_THRESHOLD,
        "passes_debt_screen": passes_debt_screen,
        "sector": sector,
        "industry": industry,
        "sector_exclusion_flag": exclusion["flag"],
        "excluded_category": exclusion["category"],
        "exclusion_reason": exclusion["reason"],
        "overall_compliant": overall_compliant,
        "unavailable_checks": unavailable,
    }


def get_sharia_compliance_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Cached wrapper around fetch_sharia_compliance_data (recommended entry point).

    Cache strategy is identical to get_fundamental_data: keyed on
    (ticker, as_of_date, data_type="sharia"), historical snapshots never expire,
    and a cached row whose schema_version != _SHARIA_SCHEMA_VERSION is treated
    as a miss and refetched.

    Filling as_of_date does NOT make the data point-in-time (see
    fetch_sharia_compliance_data / the module-level note) — the row cached under
    a historical key is the snapshot at first-call time, marked
    is_point_in_time=False.
    """
    ticker = (ticker or "").strip().upper()

    cached = get_cached(ticker, "sharia", as_of_date)
    if cached is not None and cached.get("schema_version") == _SHARIA_SCHEMA_VERSION:
        logger.info(f"Cache hit for {ticker}/sharia" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached
    if cached is not None:
        logger.info(
            f"Cache hit but stale schema "
            f"(cached v{cached.get('schema_version')!r} != v{_SHARIA_SCHEMA_VERSION}), "
            f"refetching: {ticker}/sharia" + (f"@{as_of_date}" if as_of_date else " (live)")
        )

    logger.info(f"Cache miss, fetching from yfinance: {ticker}/sharia" + (f"@{as_of_date}" if as_of_date else " (live)"))
    result = fetch_sharia_compliance_data(ticker, as_of_date=as_of_date)

    if result.get("success"):
        set_cache(ticker, "sharia", result, as_of_date)

    return result


def get_stock_context(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Menggabungkan data harga + fundamental satu saham menjadi satu dict
    konteks yang terstruktur, siap dikonsumsi tahap-tahap berikutnya
    (pembangunan graph, simulasi agent, alokasi portofolio, dst).

    Args:
        ticker: 股票代码
        as_of_date: None = konteks "live" (hari ini). Diisi string ISO
            "YYYY-MM-DD" = konteks historis pada tanggal tersebut.
            - Bagian "price" DIJAMIN tidak memuat data setelah as_of_date
              (lihat fetch_price_data).
            - Bagian "fundamental" TIDAK point-in-time — cek
              fundamental["is_point_in_time"] dan fundamental["warning"]
              sebelum memakainya untuk backtest historis (lihat catatan
              besar di docstring modul ini).
            - Bagian "sharia_compliance" (Phase 1g) juga TIDAK point-in-time —
              turunan dari totalDebt / marketCap / sector yfinance terkini.

    Returns:
        {
            "ticker": str,
            "company_name": str | None,  # Phase 1f: dari fundamental["company_name"]
                                         #   (yfinance longName / shortName). Standalone
                                         #   untuk ticker apa pun — TIDAK bergantung pada
                                         #   daftar S&P 500 Phase 1d.
            "as_of_date": str,  # nilai as_of_date apa adanya, atau "live" jika None
            "success": bool,    # True hanya jika price DAN fundamental sama-sama sukses
                                #   (sharia_compliance TIDAK ikut menentukan ini — ia
                                #   punya "success" sendiri, seperti sub-dict lain)
            "price": <hasil get_price_data(...)>,
            "fundamental": <hasil get_fundamental_data(...)>,
            "sharia_compliance": <hasil get_sharia_compliance_data(...)>,  # Phase 1g
        }
    """
    ticker = (ticker or "").strip().upper()
    price = get_price_data(ticker, as_of_date=as_of_date)
    fundamental = get_fundamental_data(ticker, as_of_date=as_of_date)
    sharia_compliance = get_sharia_compliance_data(ticker, as_of_date=as_of_date)

    return {
        "ticker": ticker,
        "company_name": fundamental.get("company_name"),
        "as_of_date": as_of_date if as_of_date is not None else "live",
        "success": bool(price.get("success")) and bool(fundamental.get("success")),
        "price": price,
        "fundamental": fundamental,
        "sharia_compliance": sharia_compliance,
    }


def _price_error(ticker: str, error: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "success": False,
        "error": error,
        "period": None,
        "as_of_date": as_of_date,
        "ohlcv": None,
        "latest_close": None,
        "latest_date": None,
        "stats": None,
        "technical_indicators": None,
    }


def _fundamental_error(ticker: str, error: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "success": False,
        "as_of_date": as_of_date,
        "schema_version": _FUNDAMENTAL_SCHEMA_VERSION,
        "is_point_in_time": None,
        "warning": None,
        "error": error,
        "company_name": None,
        "pe_ratio": None,
        "pb_ratio": None,
        "market_cap": None,
        "sector": None,
        "industry": None,
        "revenue_growth_yoy": None,
        "profit_margin": None,
        "roe": None,
        "debt_to_equity": None,
        "eps_growth": None,
        "dividend_yield": None,
        "skipped_fields": dict(_UNRELIABLE_FUNDAMENTAL_FIELDS),
    }


def _round_or_none(value: Any, ndigits: int = 4) -> Optional[float]:
    if value is None or value != value:  # value != value <=> NaN
        return None
    return round(float(value), ndigits)


def _percent_to_ratio(value: Any, ndigits: int = 6) -> Optional[float]:
    """Convert a yfinance field that is expressed as a percentage number
    (e.g. debtToEquity 78.445) into a plain ratio (0.78445). None/NaN pass
    through as None."""
    if value is None or value != value:  # value != value <=> NaN
        return None
    return round(float(value) / 100.0, ndigits)


def _pct_change_over_trading_days(closes, trading_days: int) -> Optional[float]:
    """基于最新收盘价与 trading_days 个交易日前的收盘价计算涨跌幅(%)"""
    if len(closes) <= trading_days:
        return None
    latest = closes.iloc[-1]
    past = closes.iloc[-1 - trading_days]
    if past == 0 or past != past or latest != latest:
        return None
    return round((latest - past) / past * 100, 2)


# ============================================================================
# Technical indicators — Phase 1e
# ----------------------------------------------------------------------------
# Pilihan implementasi: environment ini HANYA punya numpy + pandas (lewat
# yfinance); TIDAK ada `ta`, `pandas_ta`, maupun TA-Lib. Daripada menambah
# dependency baru hanya untuk 5 indikator standar, semuanya dihitung manual di
# sini dengan numpy + Python murni. Definisi mengikuti konvensi umum yang
# dipakai TradingView / StockCharts:
#   - EMA: rekursif dengan adjust=False (setara pandas .ewm(span=, adjust=False))
#   - RSI: Wilder's smoothing (RMA), seed = SMA `period` nilai pertama
#   - Bollinger: population std (ddof=0), %B = (harga-bawah)/(atas-bawah)
#
# KONTRAK PENTING: semua fungsi di bawah menerima list angka yang pemanggilnya
# WAJIB sudah memfilter supaya tidak memuat data setelah as_of_date. Fungsi ini
# sendiri tidak tahu-menahu soal tanggal (lihat fetch_price_data — indikator
# dihitung SETELAH filter `history.index.date <= as_of`).
# ============================================================================

def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """EMA rekursif, setara pandas Series.ewm(span=span, adjust=False).mean()."""
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(values), dtype="float64")
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _wilder_rma(values: np.ndarray, period: int) -> np.ndarray:
    """
    Wilder's smoothing / RMA (dipakai RSI):
      seed  = rata-rata sederhana `period` nilai pertama
      rma[i] = (rma[i-1] * (period-1) + x[i]) / period
    Elemen sebelum index `period-1` diisi NaN.
    """
    out = np.full(len(values), np.nan, dtype="float64")
    if len(values) < period:
        return out
    seed = float(values[:period].mean())
    out[period - 1] = seed
    for i in range(period, len(values)):
        seed = (seed * (period - 1) + values[i]) / period
        out[i] = seed
    return out


def sma(prices: List[float], window: int) -> Optional[float]:
    """
    Simple Moving Average — rata-rata `window` harga terakhir (dipakai untuk
    SMA50 dan SMA200). Return None kalau data < window (mis. SMA200 untuk saham
    yang baru IPO).
    """
    vals = [p for p in prices if p is not None and p == p]
    if window <= 0 or len(vals) < window:
        return None
    return round(sum(vals[-window:]) / window, 4)


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Relative Strength Index (Wilder). Skala 0..100; >70 lazim disebut
    overbought, <30 oversold. Return None kalau data < period + 1.
    """
    vals = [p for p in prices if p is not None and p == p]
    if len(vals) < period + 1:
        return None
    arr = np.asarray(vals, dtype="float64")
    delta = np.diff(arr)
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)
    avg_gain = _wilder_rma(gain, period)[-1]
    avg_loss = _wilder_rma(loss, period)[-1]
    if np.isnan(avg_gain) or np.isnan(avg_loss):
        return None
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def macd_signal(prices: List[float]) -> Dict[str, Optional[float]]:
    """
    MACD standar 12/26/9:
      macd      = EMA12(close) - EMA26(close)
      signal    = EMA9(macd)
      histogram = macd - signal
    Return semua None kalau data < 35 (26 + 9, minimum supaya signal bermakna).
    """
    vals = [p for p in prices if p is not None and p == p]
    if len(vals) < 26 + 9:
        return {"macd": None, "signal": None, "histogram": None}
    arr = np.asarray(vals, dtype="float64")
    macd_line = _ema(arr, 12) - _ema(arr, 26)
    signal_line = _ema(macd_line, 9)
    histogram = macd_line - signal_line
    return {
        "macd": round(float(macd_line[-1]), 4),
        "signal": round(float(signal_line[-1]), 4),
        "histogram": round(float(histogram[-1]), 4),
    }


def bollinger_position(prices: List[float], window: int = 20, num_std: float = 2.0) -> Optional[float]:
    """
    Posisi harga terakhir di dalam Bollinger Bands, sebagai %B:
      %B = (harga - band_bawah) / (band_atas - band_bawah)
    0.0 = tepat di band bawah, 1.0 = tepat di band atas. Bisa < 0 atau > 1
    kalau harga menembus band. Return None kalau data < window atau std = 0.
    """
    vals = [p for p in prices if p is not None and p == p]
    if window <= 0 or len(vals) < window:
        return None
    win = np.asarray(vals[-window:], dtype="float64")
    mid = float(win.mean())
    sd = float(win.std(ddof=0))
    if sd == 0.0:
        return None
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    return round((vals[-1] - lower) / (upper - lower), 4)


def volume_vs_avg(volumes: List[float], window: int = 20) -> Optional[float]:
    """
    Rasio volume hari terakhir terhadap rata-rata `window` hari SEBELUMNYA
    (tidak termasuk hari terakhir itu sendiri). 1.0 = seperti biasa,
    2.0 = dua kali lipat rata-rata. Return None kalau data < window + 1 atau
    rata-ratanya 0.
    """
    vals = [v for v in volumes if v is not None and v == v]
    if window <= 0 or len(vals) < window + 1:
        return None
    prior = vals[-window - 1:-1]
    avg = sum(prior) / window
    if avg == 0:
        return None
    return round(vals[-1] / avg, 4)


def _pct_diff(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    """(value - reference) / reference * 100, dibulatkan 2 desimal."""
    if value is None or reference is None or reference == 0:
        return None
    return round((value - reference) / reference * 100.0, 2)


def _pct_change_last_n(prices: List[float], n: int) -> Optional[float]:
    """Versi list dari _pct_change_over_trading_days (dipakai untuk change_1d_pct)."""
    vals = [p for p in prices if p is not None and p == p]
    if len(vals) <= n:
        return None
    latest, past = vals[-1], vals[-1 - n]
    if past == 0:
        return None
    return round((latest - past) / past * 100.0, 2)


def _compute_technical_indicators(closes: List[float], volumes: List[float]) -> Dict[str, Any]:
    """
    Rakit dict `technical_indicators` untuk fetch_price_data.

    `closes` / `volumes` HARUS sudah difilter oleh pemanggil supaya tidak
    memuat data setelah as_of_date (di fetch_price_data hal ini dijamin karena
    fungsi ini dipanggil SETELAH filter `history.index.date <= as_of`).
    """
    latest = closes[-1] if closes else None
    return {
        "price_vs_sma50": _pct_diff(latest, sma(closes, 50)),
        "price_vs_sma200": _pct_diff(latest, sma(closes, 200)),
        "rsi_14": rsi(closes, 14),
        "macd_signal": macd_signal(closes),
        "bollinger_position": bollinger_position(closes, 20, 2.0),
        "volume_vs_avg": volume_vs_avg(volumes, 20),
        "change_1d_pct": _pct_change_last_n(closes, 1),
    }


if __name__ == "__main__":
    import json
    import sqlite3

    for symbol in ["AAPL", "TSLA", "ZZZZ"]:
        print(f"\n{'=' * 60}\n{symbol}\n{'=' * 60}")

        price_result = fetch_price_data(symbol, period="1y")
        preview = dict(price_result)
        if preview.get("ohlcv"):
            preview["ohlcv"] = preview["ohlcv"][-3:]  # 只展示最近3条，避免刷屏
            preview["ohlcv_note"] = f"(共{len(price_result['ohlcv'])}条记录，此处仅展示最近3条)"
        print("[fetch_price_data]")
        print(json.dumps(preview, ensure_ascii=False, indent=2))

        fundamental_result = fetch_fundamental_data(symbol)
        print("\n[fetch_fundamental_data]")
        print(json.dumps(fundamental_result, ensure_ascii=False, indent=2))

    # --- Phase 1b: 缓存层验证 ---
    print(f"\n{'=' * 60}\nCache demo (get_price_data)\n{'=' * 60}")

    import time
    from .cache import DB_PATH

    print(f"缓存数据库文件: {DB_PATH}\n")

    print("--- 第1次调用 get_price_data('AAPL') ---")
    t0 = time.monotonic()
    first = get_price_data("AAPL")
    print(f"耗时: {time.monotonic() - t0:.3f}s, latest_close={first['latest_close']}")

    print("\n--- 第2次调用 get_price_data('AAPL')（应命中缓存，耗时应明显更短）---")
    t0 = time.monotonic()
    second = get_price_data("AAPL")
    print(f"耗时: {time.monotonic() - t0:.3f}s, latest_close={second['latest_close']}")
    assert first == second, "两次结果应完全一致（同一条缓存记录）"

    print("\n--- 调用 get_price_data('AAPL', as_of_date='2024-01-15')（应写入独立的一行）---")
    historical = get_price_data("AAPL", as_of_date="2024-01-15")
    print(f"latest_close={historical['latest_close']}")

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker, as_of_date, data_type, fetched_at FROM market_data_cache "
        "WHERE ticker = 'AAPL' ORDER BY data_type, as_of_date IS NULL DESC"
    ).fetchall()
    conn.close()
    print(f"\n当前 market_data_cache 中 AAPL 的所有行 (共{len(rows)}条):")
    for r in rows:
        print(f"  ticker={r[0]!r} as_of_date={r[1]!r} data_type={r[2]!r} fetched_at={r[3]!r}")

    # --- Phase 1c: validasi tidak ada kebocoran data masa depan (lookahead) ---
    print(f"\n{'=' * 60}\nPhase 1c: validasi no-lookahead & get_stock_context\n{'=' * 60}")

    AS_OF = "2024-06-01"

    print(f"\n--- get_stock_context('AAPL', as_of_date='{AS_OF}') ---")
    historical_ctx = get_stock_context("AAPL", as_of_date=AS_OF)
    hist_dates = [row["date"] for row in historical_ctx["price"]["ohlcv"]]
    max_hist_date = max(hist_dates) if hist_dates else None
    print(f"Jumlah baris OHLCV: {len(hist_dates)}")
    print(f"Tanggal MAKSIMUM yang ditemukan pada data harga historis: {max_hist_date}")
    assert max_hist_date is not None and max_hist_date <= AS_OF, (
        f"KEBOCORAN DATA MASA DEPAN TERDETEKSI! tanggal {max_hist_date} > as_of_date={AS_OF}"
    )
    print(f"LULUS: tidak ditemukan satu pun tanggal setelah {AS_OF} pada data harga historis.")
    print(f"fundamental.is_point_in_time = {historical_ctx['fundamental']['is_point_in_time']}")
    print(f"fundamental.warning = {historical_ctx['fundamental']['warning']}")

    print(f"\n--- get_stock_context('AAPL', as_of_date=None) (live/hari ini) ---")
    live_ctx = get_stock_context("AAPL", as_of_date=None)
    live_dates = [row["date"] for row in live_ctx["price"]["ohlcv"]]
    max_live_date = max(live_dates) if live_dates else None
    print(f"Tanggal MAKSIMUM pada data harga live: {max_live_date}")
    print(f"fundamental.is_point_in_time = {live_ctx['fundamental']['is_point_in_time']}")
    print(f"fundamental.warning = {live_ctx['fundamental']['warning']}")

    print(
        f"\nRingkasan: top-level as_of_date pada context historis = {historical_ctx['as_of_date']!r}, "
        f"pada context live = {live_ctx['as_of_date']!r}"
    )

    # --- Phase 1e: technical_indicators + validasi no-lookahead untuk indikator ---
    print(f"\n{'=' * 60}\nPhase 1e: technical_indicators & no-lookahead indikator\n{'=' * 60}")

    px = historical_ctx["price"]
    ti = px["technical_indicators"]
    print(f"\nAAPL technical_indicators @ as_of_date={AS_OF}:")
    print(f"  latest_close ({px['latest_date']})  : {px['latest_close']}")
    print(f"  price_vs_sma50   (%)          : {ti['price_vs_sma50']}")
    print(f"  price_vs_sma200  (%)          : {ti['price_vs_sma200']}")
    print(f"  rsi_14                        : {ti['rsi_14']}")
    print(f"  macd_signal                  : {ti['macd_signal']}")
    print(f"  bollinger_position (%B)       : {ti['bollinger_position']}")
    print(f"  volume_vs_avg (x 20d avg)     : {ti['volume_vs_avg']}")
    print(f"  change_1d_pct    (%)          : {ti['change_1d_pct']}")

    # Nilai SMA absolut untuk dicek manual di chart (Yahoo/TradingView) pada AS_OF:
    closes_asof = [r["close"] for r in px["ohlcv"]]
    print(f"\n  SMA50 absolut @ {AS_OF} (dari ohlcv as_of, period=1y) : {sma(closes_asof, 50)}")

    # BUKTI TIDAK ADA LOOKAHEAD:
    # Ambil seri harga LIVE (period '5y', menembus sampai hari ini). Lalu bandingkan
    #   (a) SMA dihitung HANYA dari baris <= AS_OF   -> harus == yang dipakai pipeline
    #   (b) SMA dihitung dari SEMUA baris (s.d. kini) -> harus BERBEDA jauh
    # Kalau (b) ikut cocok, berarti indikator diam-diam melihat data setelah AS_OF.
    live_5y = fetch_price_data("AAPL", period="5y")  # live, tanpa as_of_date
    rows_5y = live_5y["ohlcv"]
    closes_upto = [r["close"] for r in rows_5y if r["date"] <= AS_OF]
    closes_all = [r["close"] for r in rows_5y]
    print(f"\n  (seri 5y: {len(closes_all)} baris total, {len(closes_upto)} baris <= {AS_OF})")

    for label, win in (("SMA50", 50), ("SMA200", 200)):
        pit = sma(closes_upto, win)
        full = sma(closes_all, win)
        pipeline_pct = ti["price_vs_sma50"] if win == 50 else ti["price_vs_sma200"]
        pit_pct = _pct_diff(closes_upto[-1], pit)
        full_pct = _pct_diff(closes_all[-1], full)
        print(f"\n  {label}: absolut @ {AS_OF} (manual) = {pit} | absolut s.d. kini = {full}")
        print(f"       price_vs_{label.lower()}: pipeline={pipeline_pct}  manual@{AS_OF}={pit_pct}  s.d.kini={full_pct}")
        assert pipeline_pct is not None and pit_pct is not None
        assert abs(pipeline_pct - pit_pct) < 0.05, (
            f"MISMATCH {label}: pipeline as_of={pipeline_pct} != hitung manual s.d. {AS_OF}={pit_pct}"
        )
        assert full_pct is None or abs(pipeline_pct - full_pct) > 0.5, (
            f"BOCOR {label}: pipeline as_of={pipeline_pct} justru == hitung pakai data terkini={full_pct} "
            f"-> indikator melihat data setelah {AS_OF}!"
        )

    print(
        f"\nLULUS: SMA50 & SMA200 pada as_of_date={AS_OF} cocok dengan hitung manual "
        f"yang HANYA memakai data s.d. {AS_OF}, dan jelas berbeda dari hitung memakai "
        f"data terkini -> tidak ada lookahead pada technical_indicators."
    )

    # --- Phase 1f: all fundamental fields (old + new) + company_name ---
    print(f"\n{'=' * 60}\nPhase 1f: full fundamental + company_name\n{'=' * 60}")

    ctx_1f = get_stock_context("AAPL", as_of_date=AS_OF)
    fund = ctx_1f["fundamental"]

    print(f"\nget_stock_context('AAPL', as_of_date='{AS_OF}')")
    print(f"  top-level company_name        : {ctx_1f['company_name']!r}")
    print(f"\n  fundamental.company_name      : {fund['company_name']!r}")
    print(f"  fundamental.schema_version    : {fund['schema_version']}")
    print(f"  fundamental.is_point_in_time  : {fund['is_point_in_time']}")
    print(f"  fundamental.warning           : {fund['warning']}")

    print("\n  -- OLD fields (Phase 1a) --")
    for key in ("pe_ratio", "pb_ratio", "market_cap", "sector", "industry",
                "revenue_growth_yoy", "profit_margin"):
        print(f"    {key:22}: {fund.get(key)!r}")

    print("\n  -- NEW fields (Phase 1f) --")
    for key in ("roe", "debt_to_equity", "eps_growth", "dividend_yield"):
        print(f"    {key:22}: {fund.get(key)!r}")

    # debt_to_equity: show yfinance's raw percent-scale number next to the
    # normalized ratio we actually store.
    raw_dte = yf.Ticker("AAPL").info.get("debtToEquity")
    print(f"\n    debt_to_equity: yfinance raw = {raw_dte} (percent scale) "
          f"-> stored = {fund['debt_to_equity']} (plain ratio, x{fund['debt_to_equity']:.2f})")

    print("\n  -- SKIPPED fields (skipped_fields) --")
    for name, reason in fund.get("skipped_fields", {}).items():
        print(f"    {name:22}: {reason}")

    assert ctx_1f["company_name"], "company_name empty -> longName not picked up"
    assert all(k in fund for k in ("roe", "debt_to_equity", "eps_growth", "dividend_yield")), \
        "Phase 1f fields missing from result"
    assert "peg_ratio" not in fund, "PEG should be skipped, not returned"
    assert fund["is_point_in_time"] is False, "as_of fundamental fields must be is_point_in_time=False"
    # debt_to_equity must be a plain ratio now, not a percent number: a >20x D/E
    # would be extraordinary, so anything that large means the /100 didn't happen.
    assert fund["debt_to_equity"] is None or fund["debt_to_equity"] < 20, (
        f"debt_to_equity={fund['debt_to_equity']} looks like a percent number, not a ratio"
    )
    assert raw_dte is None or abs(fund["debt_to_equity"] - raw_dte / 100.0) < 1e-6, \
        "stored debt_to_equity is not yfinance debtToEquity / 100"
    print(f"\nPASS: company_name + 4 new fundamental fields present, PEG skipped, "
          f"debt_to_equity normalized to a ratio, all carry is_point_in_time=False.")

    # --- Phase 1g: Sharia compliance screening — AAPL vs JPM side by side ---
    print(f"\n{'=' * 60}\nPhase 1g: sharia_compliance (AAOIFI SS-21)\n{'=' * 60}")
    print(f"  debt screen threshold: debt_to_market_cap < "
          f"{_SHARIA_DEBT_TO_MCAP_THRESHOLD} (corrected 0.30 -> 0.33 per project ref)")

    def _show_sharia(sym: str) -> Dict[str, Any]:
        ctx = get_stock_context(sym, as_of_date=AS_OF)
        sc = ctx["sharia_compliance"]
        print(f"\n  ---- {sym}  (get_stock_context('{sym}', as_of_date='{AS_OF}')['sharia_compliance']) ----")
        print(f"    success                       : {sc['success']}")
        print(f"    standard                      : {sc['standard']}")
        print(f"    sector / industry             : {sc['sector']!r} / {sc['industry']!r}")
        print(f"    total_debt                    : {sc['total_debt']!r}")
        print(f"    market_cap                    : {sc['market_cap']!r}")
        print(f"    debt_to_market_cap            : {sc['debt_to_market_cap']!r}  "
              f"(threshold {sc['debt_to_market_cap_threshold']}, distinct from "
              f"fundamental.debt_to_equity={ctx['fundamental'].get('debt_to_equity')!r})")
        print(f"    passes_debt_screen            : {sc['passes_debt_screen']!r}")
        print(f"    sector_exclusion_flag         : {sc['sector_exclusion_flag']!r}")
        print(f"    excluded_category             : {sc['excluded_category']!r}")
        print(f"    exclusion_reason              : {sc['exclusion_reason']!r}")
        print(f"    overall_compliant (PARTIAL)   : {sc['overall_compliant']!r}")
        print(f"    is_point_in_time              : {sc['is_point_in_time']!r}")
        print(f"    warning                       : {sc['warning']}")
        print(f"    unavailable_checks            :")
        for name, reason in sc["unavailable_checks"].items():
            print(f"        - {name}: {reason}")
        return sc

    sc_aapl = _show_sharia("AAPL")
    sc_jpm = _show_sharia("JPM")
    # H (Hyatt Hotels) — live debt_to_market_cap has historically sat right around
    # 0.30-0.31: a real borderline case that FAILS the old <0.30 rule but PASSES
    # the corrected <0.33 rule. Not sector-excluded (Lodging).
    sc_h = _show_sharia("H")

    print(f"\n  {'':24}{'AAPL':>16}{'JPM':>26}{'H (borderline)':>26}")
    for key in ("sector", "sector_exclusion_flag", "excluded_category",
                "debt_to_market_cap", "passes_debt_screen", "overall_compliant"):
        print(f"  {key:24}{str(sc_aapl[key]):>16}{str(sc_jpm[key]):>26}{str(sc_h[key]):>26}")

    assert sc_aapl["sector_exclusion_flag"] is False, \
        "AAPL (Technology) must NOT be sector-excluded"
    assert sc_aapl["excluded_category"] is None and sc_aapl["exclusion_reason"] is None
    assert sc_aapl["passes_debt_screen"] is True and sc_aapl["overall_compliant"] is True
    assert sc_jpm["sector_exclusion_flag"] is True, \
        "JPM (conventional bank) MUST be sector-excluded"
    assert sc_jpm["excluded_category"] == "conventional_finance", \
        f"JPM should be excluded as conventional_finance, got {sc_jpm['excluded_category']!r}"
    assert "bank" in (sc_jpm["exclusion_reason"] or "").lower() \
        or "financ" in (sc_jpm["exclusion_reason"] or "").lower()
    assert sc_jpm["overall_compliant"] is False
    assert sc_aapl["is_point_in_time"] is False and sc_jpm["is_point_in_time"] is False

    # threshold sanity + borderline behaviour
    assert sc_aapl["debt_to_market_cap_threshold"] == 0.33
    if sc_h["debt_to_market_cap"] is not None:
        expected_h = sc_h["debt_to_market_cap"] < 0.33
        assert sc_h["passes_debt_screen"] is expected_h
        assert sc_h["sector_exclusion_flag"] is False
        assert sc_h["overall_compliant"] is expected_h
        old_rule = sc_h["debt_to_market_cap"] < 0.30
        note = ("SAME under both thresholds"
                if old_rule == expected_h
                else f"DIFFERS: old<0.30 -> {old_rule}, corrected<0.33 -> {expected_h}")
        print(f"\n  H debt_to_market_cap = {sc_h['debt_to_market_cap']}  ({note})")

    print(f"\nPASS: sector exclusion discriminates AAPL (allowed) vs JPM "
          f"(excluded: {sc_jpm['excluded_category']}); debt screen uses <0.33; "
          f"both is_point_in_time=False.")
