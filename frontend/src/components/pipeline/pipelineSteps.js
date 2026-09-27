// Steps as the UI shows them. The backend (app/news_pipeline/run_store.py)
// keeps 8 finer steps for resume/history; `keys` maps each UI step onto them.
// Reality seed (backend "seed") and simulation prompt ("prompt") have no UI
// of their own — they are part of "MiroFish Simulation".
export const PIPELINE_STEPS = [
  { key: 'universe', keys: ['universe'], title: 'Ticker Universe', desc: 'Filter S&P 500 by sector + market cap at as_of' },
  { key: 'news', keys: ['news'], title: 'Finnhub News', desc: 'Fetch 90 days of news per ticker → compact txt_berita' },
  { key: 'mirofish', keys: ['seed', 'prompt', 'simulation'], title: 'MiroFish Simulation', desc: 'txt_berita as reality seed + simulation prompt → graph → agents → simulation → report' },
  { key: 'report_json', keys: ['report_json'], title: 'Report → JSON', desc: 'Convert the generated report into structured JSON via LLM' },
  { key: 'consensus', keys: ['consensus'], title: 'Consensus', desc: 'Aggregate the JSON into a consensus per ticker' },
  { key: 'performance', keys: ['performance'], title: 'Performance Report', desc: 'Measure how the selected tickers performed after as_of' }
]

/** Aggregated status of a UI step from the backend manifest `steps` */
export function uiStepStatus(steps, uiStep) {
  const statuses = uiStep.keys.map(k => steps?.[k]?.status || 'pending')
  if (statuses.every(s => s === 'completed')) return 'completed'
  if (statuses.includes('failed')) return 'failed'
  if (statuses.includes('running')) return 'running'
  if (statuses.includes('paused')) return 'paused'
  if (statuses.includes('completed')) return 'running' // partly done (e.g. seed fed, prompt pending)
  return 'pending'
}

/** 1-based UI step number the run is at (first UI step not completed) */
export function uiCurrentStep(run) {
  const i = PIPELINE_STEPS.findIndex(s => uiStepStatus(run?.steps, s) !== 'completed')
  return i === -1 ? PIPELINE_STEPS.length : i + 1
}

/** First backend error inside a UI step, if any */
export function uiStepError(steps, uiStep) {
  return uiStep.keys.map(k => steps?.[k]?.error).find(Boolean) || null
}
