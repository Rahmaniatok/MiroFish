import service from './index'

/**
 * Screen the S&P 500 universe by GICS sector + market-cap tier (Phase 1d,
 * exposed via Phase 7a's GET /api/universe/screen).
 * @param {Object} params
 * @param {string[]} [params.sectors] - GICS sector names, e.g. "Information Technology"
 * @param {string[]} [params.marketCapTiers] - subset of ["mega", "large", "mid", "small"]
 * @param {string} [params.asOfDate] - "YYYY-MM-DD"; omitted = live
 * @returns {Promise<Array>} the screened candidate list
 */
export function screenUniverse({ sectors, marketCapTiers, asOfDate } = {}) {
  const params = {}
  if (sectors && sectors.length) params.sectors = sectors.join(',')
  if (marketCapTiers && marketCapTiers.length) params.market_cap_tiers = marketCapTiers.join(',')
  if (asOfDate) params.as_of_date = asOfDate

  return service({
    url: '/api/universe/screen',
    method: 'get',
    params
  })
}
