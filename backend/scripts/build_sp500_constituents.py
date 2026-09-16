"""
一次性构建脚本：从公开来源（Wikipedia）抓取 S&P 500 成分股列表，
写入 backend/app/data_layer/sp500_constituents.json（ticker + name + sector）。

不在请求时调用 —— 运行方式：
    cd backend && python -m scripts.build_sp500_constituents

输出的 JSON 被 app.data_layer.universe.screen_universe() 作为"筛选前"的
静态参考数据使用（零 API 调用即可按 sector 预过滤，省掉不必要的 yfinance 请求）。
"""

import io
import json
import os

import pandas as pd
import requests

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "data_layer", "sp500_constituents.json"
)


def _to_yfinance_ticker(symbol: str) -> str:
    """Wikipedia 用 '.' 表示股票类别（如 BRK.B），yfinance 用 '-'（BRK-B）"""
    return symbol.strip().replace(".", "-")


def build() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(WIKIPEDIA_URL, headers=headers, timeout=30)
    response.raise_for_status()

    table = pd.read_html(io.StringIO(response.text))[0]

    constituents = [
        {
            "ticker": _to_yfinance_ticker(row["Symbol"]),
            "name": str(row["Security"]).strip(),
            "sector": str(row["GICS Sector"]).strip(),
        }
        for _, row in table.iterrows()
    ]
    constituents.sort(key=lambda c: c["ticker"])
    return constituents


def main() -> None:
    constituents = build()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(constituents, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(constituents)} constituents to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
