"""
Universe 筛选 API 路由
取代原本 /api/graph/ontology/generate 的文档分析流程 —— 数据源改为 yfinance，
不做任何 LLM 调用，纯数据 fetch & filter。
"""

import traceback

from flask import request, jsonify

from . import universe_bp
from ..data_layer.universe import screen_and_upsert_universe
from ..utils.logger import get_logger

logger = get_logger('mirofish.api')


@universe_bp.route('/screen', methods=['POST'])
def screen_universe_endpoint():
    """
    筛选 S&P 500 股票并 upsert 为 StockNode

    请求（JSON）：
        {
            "sectors": ["Technology"],   // 可选，默认不限制（全部 sector）
            "min_market_cap": 1e11       // 可选，默认不限制
        }

    返回：
        {
            "success": true,
            "data": {
                "candidates_prefiltered": int,  // sector 预过滤后、发起 yfinance 请求前的数量
                "fetched_ok": int,               // yfinance 成功返回的数量
                "fetch_failed": int,             // yfinance 失败/数据不完整而跳过的数量
                "upserted_count": int,
                "stock_nodes": [{ticker, name, sector, market_cap, cluster_id, last_updated}, ...]
            }
        }
    """
    try:
        data = request.get_json(silent=True) or {}

        sectors = data.get('sectors')
        if sectors is not None:
            if not isinstance(sectors, list) or not all(isinstance(s, str) for s in sectors):
                return jsonify({
                    "success": False,
                    "error": "sectors must be a JSON array of strings"
                }), 400

        min_market_cap = data.get('min_market_cap')
        if min_market_cap is not None:
            if not isinstance(min_market_cap, (int, float)) or isinstance(min_market_cap, bool):
                return jsonify({
                    "success": False,
                    "error": "min_market_cap must be a number"
                }), 400
            min_market_cap = float(min_market_cap)

        logger.info(f"筛选 universe: sectors={sectors}, min_market_cap={min_market_cap}")
        result = screen_and_upsert_universe(sectors=sectors, min_market_cap=min_market_cap)

        return jsonify({
            "success": True,
            "data": result,
        })

    except Exception as error:
        logger.exception("Universe 筛选失败")
        return jsonify({
            "success": False,
            "error": str(error),
            "traceback": traceback.format_exc(),
        }), 500
