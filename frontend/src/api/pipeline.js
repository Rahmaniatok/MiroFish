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

// ---- step 2: news ----

/** News step status: per-ticker progress + stats, live fetch info, txt meta */
export function getNewsStatus(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/news`, method: 'get' })
}

/** Start or resume the Finnhub fetch (already-fetched tickers are skipped) */
export function startNews(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/news/start`, method: 'post' })
}

export function pauseNews(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/news/pause`, method: 'post' })
}

/** Articles of one ticker with keep/drop decision; view = 'all' | 'kept' */
export function getNewsArticles(runId, ticker, view = 'all') {
  return service({ url: `/api/pipeline/runs/${runId}/news/articles`, method: 'get', params: { ticker, view } })
}

/** Rebuild txt_berita with another per-ticker cap (no refetch) */
export function compactNews(runId, cap) {
  return service({ url: `/api/pipeline/runs/${runId}/news/compact`, method: 'post', data: { cap } })
}

export function getNewsTxt(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/news/txt`, method: 'get' })
}

export const newsTxtDownloadUrl = (runId) =>
  `${service.defaults.baseURL}/api/pipeline/runs/${runId}/news/txt?download=1`

// ---- step 3: reality seed ----

/** Feed txt_berita into a MiroFish project as its reality seed (idempotent) */
export function feedSeed(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/seed`, method: 'post' })
}

// ---- step 4: simulation prompt ----

/** The simulation prompt (simulation_requirement) built from ticker_universe */
export function getPrompt(runId) {
  return service({ url: `/api/pipeline/runs/${runId}/prompt`, method: 'get' })
}
