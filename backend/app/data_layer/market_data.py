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

# yfinance `.info` 中已知覆盖率低或口径不稳定的基本面字段：跳过不返回，
# 避免调用方把 None 误解读为"没有增长/没有股息"等业务含义。
_UNRELIABLE_FUNDAMENTAL_FIELDS = {
    "trailingPegRatio": "经常为 None 或异常值，且不同行业 PEG 计算口径不一致",
    "pegRatio": "同上，且该字段已在部分 yfinance 版本中弃用",
    "earningsGrowth": "非美股/小盘股大量为 None，覆盖率低",
    "dividendYield": "不同 yfinance 版本返回的单位不一致（如 0.5 vs 0.005），且无股息股票为 None",
    "enterpriseToRevenue": "覆盖率低，部分行业（如金融）该指标无意义",
    "enterpriseToEbitda": "同上",
    "targetMeanPrice": "分析师目标价滞后且样本量不透明，不适合作为量化输入",
}


# Dipakai sebagai pengganti "tanpa batas bawah" untuk period="max" (lihat
# _resolve_lookback_start) — lebih tua dari IPO saham manapun yang realistis
_EARLIEST_POSSIBLE_DATE = date(1900, 1, 1)


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


def fetch_price_data(ticker: str, period: str = "1y", as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    获取股票历史价格数据(OHLCV)及基础衍生统计指标

    Args:
        ticker: 股票代码，如 "AAPL"
        period: yfinance 支持的周期字符串，如 "1mo"/"3mo"/"1y"/"5y"/"max"/"ytd"。
            无论 live 还是 as_of_date 模式，都是作为"向前回溯多久"的窗口长度。
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
            }
        }
        失败: {"ticker": str, "success": False, "error": str, "period": None,
               "as_of_date": str | None, "ohlcv": None, "latest_close": None,
               "latest_date": None, "stats": None}
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
            history = yf.Ticker(ticker).history(period=period)
        else:
            start = _resolve_lookback_start(as_of, period)
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
        # Jaring pengaman tambahan (defense in depth): walaupun `end` di atas
        # sudah dihitung supaya tidak melewati as_of_date, di sini kita filter
        # eksplisit sekali lagi berdasarkan tanggal tiap baris. Tujuannya agar
        # jaminan "tidak ada kebocoran data masa depan" TIDAK diam-diam
        # bergantung pada detail exclusivity/timezone `end` milik yfinance
        # yang berpotensi berubah di versi mendatang.
        history = history[history.index.date <= as_of]
        if history.empty:
            return _price_error(
                ticker,
                f"股票代码 '{ticker}' 在 as_of_date={as_of_date} 或之前没有历史数据",
                as_of_date,
            )

    ohlcv: List[Dict[str, Any]] = []
    for row_date, row in history.iterrows():
        volume = row.get("Volume")
        ohlcv.append({
            "date": row_date.strftime("%Y-%m-%d"),
            "open": _round_or_none(row.get("Open")),
            "high": _round_or_none(row.get("High")),
            "low": _round_or_none(row.get("Low")),
            "close": _round_or_none(row.get("Close")),
            "volume": int(volume) if volume == volume else None,  # volume != volume <=> NaN
        })

    closes = history["Close"]
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
        "latest_date": history.index[-1].strftime("%Y-%m-%d"),
        "stats": stats,
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
        成功: {
            "ticker": str, "success": True, "error": None,
            "as_of_date": str | None, "is_point_in_time": bool, "warning": str | None,
            "pe_ratio": float | None, "pb_ratio": float | None,
            "market_cap": int | None, "sector": str | None, "industry": str | None,
            "revenue_growth_yoy": float | None, "profit_margin": float | None,
            "skipped_fields": {字段名: 跳过原因},
        }
        失败: {"ticker": str, "success": False, "error": str, ...其余字段为 None,
               "skipped_fields": {...}}
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
        "is_point_in_time": is_point_in_time,
        "warning": warning,
        "pe_ratio": pe_ratio,
        "pb_ratio": info.get("priceToBook"),
        "market_cap": info.get("marketCap"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "revenue_growth_yoy": info.get("revenueGrowth"),
        "profit_margin": info.get("profitMargins"),
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
    """
    ticker = (ticker or "").strip().upper()

    cached = get_cached(ticker, "price", as_of_date)
    if cached is not None:
        logger.info(f"Cache hit for {ticker}/price" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached

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
    if cached is not None:
        logger.info(f"Cache hit for {ticker}/fundamental" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached

    logger.info(f"Cache miss, fetching from yfinance: {ticker}/fundamental" + (f"@{as_of_date}" if as_of_date else " (live)"))
    result = fetch_fundamental_data(ticker, as_of_date=as_of_date)

    if result.get("success"):
        set_cache(ticker, "fundamental", result, as_of_date)

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

    Returns:
        {
            "ticker": str,
            "as_of_date": str,  # nilai as_of_date apa adanya, atau "live" jika None
            "success": bool,    # True hanya jika price DAN fundamental sama-sama sukses
            "price": <hasil get_price_data(...)>,
            "fundamental": <hasil get_fundamental_data(...)>,
        }
    """
    ticker = (ticker or "").strip().upper()
    price = get_price_data(ticker, as_of_date=as_of_date)
    fundamental = get_fundamental_data(ticker, as_of_date=as_of_date)

    return {
        "ticker": ticker,
        "as_of_date": as_of_date if as_of_date is not None else "live",
        "success": bool(price.get("success")) and bool(fundamental.get("success")),
        "price": price,
        "fundamental": fundamental,
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
    }


def _fundamental_error(ticker: str, error: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "success": False,
        "as_of_date": as_of_date,
        "is_point_in_time": None,
        "warning": None,
        "error": error,
        "pe_ratio": None,
        "pb_ratio": None,
        "market_cap": None,
        "sector": None,
        "industry": None,
        "revenue_growth_yoy": None,
        "profit_margin": None,
        "skipped_fields": dict(_UNRELIABLE_FUNDAMENTAL_FIELDS),
    }


def _round_or_none(value: Any, ndigits: int = 4) -> Optional[float]:
    if value is None or value != value:  # value != value <=> NaN
        return None
    return round(float(value), ndigits)


def _pct_change_over_trading_days(closes, trading_days: int) -> Optional[float]:
    """基于最新收盘价与 trading_days 个交易日前的收盘价计算涨跌幅(%)"""
    if len(closes) <= trading_days:
        return None
    latest = closes.iloc[-1]
    past = closes.iloc[-1 - trading_days]
    if past == 0 or past != past or latest != latest:
        return None
    return round((latest - past) / past * 100, 2)


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
