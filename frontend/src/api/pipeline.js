import service from './index'

// News pipeline (/api/pipeline) — resumable 8-step runs.
// Every response is {success, data}; the interceptor rejects success=false.

/** Sector list, market-cap tiers, as_of bounds and data caveats */
export function getUniverseOptions() {
  return service({ url: '/api/pipeline/universe/options', method: 'get' })
}

/** Validate an as_of date → {as_of_date, warnings} (rejects when unusable) */
export function validateAsOf(asOfDate) {
  return service({ url: '/api/pipeline/universe/validate-as-of', method: 'get', params: { as_of_date: asOfDate } })
}

/** Instant S&P 500 members for the chosen sectors (no market data) */
export function getConstituents(sectors = []) {
  return service({ url: '/api/pipeline/universe/constituents', method: 'get', params: { sectors: sectors.join(',') } })
}

/** Start a background market-cap screen → {task_id} */
export function startUniverseScreen(data) {
  return service({ url: '/api/pipeline/universe/screen', method: 'post', data })
}

/** Poll a screen: progress_detail streams passed/filtered/skipped rows */
export function getUniverseScreen(taskId) {
  return service({ url: `/api/pipeline/universe/screen/${taskId}`, method: 'get' })
}

/**
 * Lock a finished screen as ticker_universe → new run (step 1 completed)
 * @param {Object} data - {task_id, name, market_cap_tiers, excluded_tickers}
 */
export function createRun(data) {
  return service({ url: '/api/pipeline/runs', method: 'post', data })
}

export function listRuns() {
  return service({ url: '/api/pipeline/runs', method: 'get' })
}

/** Run manifest + step 1 universe artifact */
export function getRun(runId) {
  return service({ url: `/api/pipeline/runs/${runId}`, method: 'get' })
}

/** Persistent run history (run.log) */
export function getRunLog(runId, limit) {
  return service({ url: `/api/pipeline/runs/${runId}/log`, method: 'get', params: limit ? { limit } : {} })
}
