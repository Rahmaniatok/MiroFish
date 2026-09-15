import service from './index'

/**
 * Fetch the entity graph (Phase 2b/2c FilteredEntities) for one ticker
 * (Phase 7a's GET /api/portfolio/graph, added for Phase 7b's Workbench).
 * @param {Object} data
 * @param {string} data.ticker
 * @param {string|null} [data.asOfDate]
 * @returns {Promise<Object>} {entities, entity_types, total_count, filtered_count}
 */
export function fetchTickerGraph({ ticker, asOfDate = null } = {}) {
  const params = { ticker }
  if (asOfDate) params.as_of_date = asOfDate
  return service({
    url: '/api/portfolio/graph',
    method: 'get',
    params
  })
}

/**
 * Screen + rank candidates and mean-variance optimize them (Phase 5/5e,
 * exposed via Phase 7a's POST /api/portfolio/build).
 * @param {Object} data
 * @param {string[]} data.candidateTickers
 * @param {string|null} [data.asOfDate]
 * @param {number} [data.topK]
 * @param {number} [data.maxWeight]
 * @param {string} [data.model] - "max_sharpe" (default) | "min_variance" | "hrp"
 * @returns {Promise<Object>} the full build_portfolio() result
 */
export function buildPortfolio({ candidateTickers, asOfDate = null, topK = 25, maxWeight = 0.20, model = 'max_sharpe' }) {
  return service({
    url: '/api/portfolio/build',
    method: 'post',
    data: {
      candidate_tickers: candidateTickers,
      as_of_date: asOfDate,
      top_k: topK,
      max_weight: maxWeight,
      model
    }
  })
}

/**
 * Ask a plain-English question about an already-built portfolio (Phase 6a,
 * exposed via Phase 7a's POST /api/portfolio/ask).
 * @param {string} question
 * @param {Object} portfolio - a buildPortfolio() result
 * @returns {Promise<{answer: string}>}
 */
export function askPortfolio(question, portfolio) {
  return service({
    url: '/api/portfolio/ask',
    method: 'post',
    data: { question, portfolio }
  })
}

/**
 * Run a fresh multi-round agent debate for one ticker in the portfolio
 * (Phase 4/6b, exposed via Phase 7a's POST /api/portfolio/debate).
 * @param {Object} data
 * @param {string} data.ticker
 * @param {Object} data.portfolio - a buildPortfolio() result
 * @param {number} [data.rounds]
 * @param {boolean} [data.useLlm]
 * @returns {Promise<Object>} the DebateTranscript (to_dict() shape)
 */
export function debatePortfolio({ ticker, portfolio, rounds = 3, useLlm = true }) {
  return service({
    url: '/api/portfolio/debate',
    method: 'post',
    data: { ticker, portfolio, rounds, use_llm: useLlm }
  })
}
