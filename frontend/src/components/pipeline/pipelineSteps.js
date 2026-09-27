// Mirrors backend app/news_pipeline/run_store.py STEPS (order = step number).
export const PIPELINE_STEPS = [
  { key: 'universe', title: 'Ticker Universe', desc: 'Filter S&P 500 by sector + market cap at as_of' },
  { key: 'news', title: 'Finnhub News', desc: 'Fetch 90 days of news per ticker → compact txt_berita' },
  { key: 'seed', title: 'Reality Seed', desc: 'Feed txt_berita into MiroFish as the reality seed (graph build)' },
  { key: 'prompt', title: 'Simulation Prompt', desc: 'Build the simulation prompt from ticker_universe' },
  { key: 'simulation', title: 'MiroFish Simulation', desc: 'Prepare agents and run the OASIS simulation' },
  { key: 'report_json', title: 'Report → JSON', desc: 'Convert the generated report into structured JSON via LLM' },
  { key: 'consensus', title: 'Consensus', desc: 'Aggregate the JSON into a consensus per ticker' },
  { key: 'performance', title: 'Performance Report', desc: 'Measure how the selected tickers performed after as_of' }
]
