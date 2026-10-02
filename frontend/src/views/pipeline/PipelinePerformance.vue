<template>
  <div class="perf">
    <!-- Navbar -->
    <nav class="navbar">
      <div class="nav-left">
        <div class="nav-brand" @click="router.push('/')">MIROFISH</div>
        <span class="nav-tag">NEWS PIPELINE</span>
        <span class="crumb" v-if="run">/ <b>{{ run.name }}</b></span>
      </div>
      <div class="nav-right">
        <span class="nav-link" @click="router.push(`/pipeline/${runId}`)">Workspace</span>
        <span class="nav-link" @click="router.push('/pipeline')">All runs</span>
      </div>
    </nav>

    <!-- Header -->
    <header class="head">
      <div>
        <div class="eyebrow"><span class="orange-sq"></span> STEP 06 / 06 · PERFORMANCE</div>
        <h1 class="title">Performance <span class="accent">Report</span></h1>
        <p class="sub" v-if="res">
          Bought at the close of <b>{{ res.base_date }}</b> (first trading day after as_of {{ res.as_of_date }}), held to
          <b>{{ res.end_date }}</b> · {{ res.trading_days }} trading days · risk-free {{ pct(res.risk_free_rate, 2) }} (^IRX)
        </p>
        <p class="sub" v-else>Consensus and every persona's picks, measured from as_of until today.</p>
      </div>
      <div class="head-side">
        <PipelineStepRail v-if="run" :steps="run.steps" />
        <div class="controls">
          <span v-if="res" class="updated">prices as of {{ res.end_date }}</span>
          <button class="main-btn" :disabled="building || !canBuild" @click="build">
            <span v-if="building" class="spin"></span>
            {{ building ? 'FETCHING PRICES…' : res ? '↻ UPDATE TO TODAY' : 'BUILD REPORT' }}
          </button>
        </div>
      </div>
    </header>

    <div v-if="error" class="banner error">{{ error }}</div>
    <div v-if="stale" class="banner warn">The consensus changed after this report was built — press <b>Update to today</b> to rebuild it.</div>
    <div v-if="!canBuild && run" class="banner warn">The consensus (step 05) is not built yet.</div>
    <div v-for="w in res?.warnings || []" :key="w" class="banner note">{{ w }}</div>

    <template v-if="res">
      <!-- KPI: consensus -->
      <section class="kpis">
        <div class="kpi hero">
          <div class="kpi-label">Consensus · total return</div>
          <div class="kpi-value">{{ pct(cons.metrics.total_return) }}</div>
          <div class="kpi-sub">{{ cons.tickers.join(' · ') }}</div>
        </div>
        <div v-for="b in versus" :key="b.key" class="kpi">
          <div class="kpi-label">Excess vs {{ b.label }}</div>
          <div class="kpi-value" :class="b.diff >= 0 ? 'good' : 'bad'" :title="`Consensus ${pct(cons.metrics.total_return)} − ${b.label} ${pct(b.ret)}`">
            <span class="icon">{{ b.diff >= 0 ? '▲' : '▼' }}</span>{{ pp(b.diff) }}
          </div>
          <div class="kpi-sub">{{ b.label }} itself: {{ pct(b.ret) }} · {{ b.diff >= 0 ? 'beaten' : 'not beaten' }}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Sharpe · Sortino</div>
          <div class="kpi-value">{{ num(cons.metrics.sharpe) }} <small>/ {{ num(cons.metrics.sortino) }}</small></div>
          <div class="kpi-sub">max drawdown {{ pct(cons.metrics.max_drawdown) }}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Prompt horizon</div>
          <div class="kpi-value">{{ pct(cons.horizons['4w']?.return) }} <small>/ {{ pct(cons.horizons['6w']?.return) }}</small></div>
          <div class="kpi-sub">at 4 weeks / 6 weeks</div>
        </div>
      </section>

      <!-- legend / filters -->
      <section class="legend">
        <div v-for="g in groups" :key="g.kind" class="lg-group">
          <span class="lg-title">{{ g.title }}</span>
          <button
            v-for="p in g.items"
            :key="p.key"
            class="lg-chip"
            :class="{ off: !visible.has(p.key), sel: selected === p.key }"
            @click="toggle(p.key)"
            @mouseenter="focus = p.key"
            @mouseleave="focus = null"
          >
            <svg width="22" height="8"><line x1="1" y1="4" x2="21" y2="4" :stroke="style(p).color" :stroke-width="Math.min(3, style(p).width)" :stroke-dasharray="style(p).dash || null" stroke-linecap="round" /></svg>
            {{ p.label }}
          </button>
          <button v-if="g.kind === 'persona'" class="lg-mini" @click="setGroup('persona', !g.items.every(p => visible.has(p.key)))">
            {{ g.items.every(p => visible.has(p.key)) ? 'hide all' : 'show all' }}
          </button>
        </div>
      </section>

      <!-- charts -->
      <section class="card">
        <div class="card-head">
          <h2>Cumulative return</h2>
          <span class="card-note">buy-and-hold · dashed orange lines = the prompt's 4w / 6w horizon</span>
        </div>
        <PerfLineChart :dates="res.dates" :series="chartSeries('series')" :horizons="res.horizons" :focus="focus" :height="380" aria-label="Cumulative return per portfolio" />
        <div class="card-head sub-head">
          <h2>Drawdown</h2>
          <span class="card-note">distance below the running peak</span>
        </div>
        <PerfLineChart :dates="res.dates" :series="chartSeries('drawdown')" :focus="focus" :height="170" aria-label="Drawdown per portfolio" />
      </section>

      <!-- leaderboard -->
      <section class="card">
        <div class="card-head">
          <h2>Leaderboard</h2>
          <span class="card-note">click a row for its holdings & correlation · best value per column in bold</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="l">Portfolio</th>
                <th v-for="c in COLS" :key="c.key" :class="{ sorted: sortKey === c.key }" @click="sortBy(c.key)" :title="c.help">
                  {{ c.label }}<span v-if="sortKey === c.key">{{ sortDir > 0 ? '↑' : '↓' }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in sortedRows" :key="p.key" :class="[p.kind, { sel: selected === p.key }]" @click="selected = p.key">
                <td class="l">
                  <span class="row-sw" :style="{ background: style(p).color }"></span>
                  <span class="row-name">{{ p.label }}</span>
                  <span class="kind">{{ p.kind === 'persona' ? p.tickers.length + ' picks' : p.kind === 'consensus' ? p.tickers.length + ' picks' : 'benchmark' }}</span>
                </td>
                <td v-for="c in COLS" :key="c.key" :class="{ best: best[c.key] === p.key, neg: c.signed && val(p, c.key) < 0 }">
                  <span v-if="c.key === 'total_return'" class="ret-bar"><span :style="retBar(val(p, c.key))"></span></span>
                  {{ c.fmt(val(p, c.key)) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- holdings + correlation -->
      <section class="split">
        <div class="card">
          <div class="card-head">
            <h2>Holdings · {{ sel.label }}</h2>
            <span class="card-note">weight · own return · contribution to the portfolio</span>
          </div>
          <div class="holdings">
            <div v-for="h in sel.holdings" :key="h.ticker" class="h-row">
              <span class="h-tk">{{ h.ticker }}</span>
              <span class="h-w"><span class="wbar"><span :style="{ width: h.weight * 100 + '%' }"></span></span>{{ pct(h.weight, 1, false) }}</span>
              <span class="h-r" :class="h.return >= 0 ? 'good' : 'bad'">{{ h.return >= 0 ? '▲' : '▼' }} {{ pct(h.return) }}</span>
              <span class="h-c">
                <span class="cbar">
                  <span class="mid"></span>
                  <span class="fill" :class="h.contribution >= 0 ? 'pos' : 'neg'" :style="contribBar(h.contribution)"></span>
                </span>
                {{ pct(h.contribution, 2) }}
              </span>
            </div>
          </div>
          <div class="metric-grid">
            <div v-for="c in COLS.slice(0, 9)" :key="c.key" class="mg">
              <span>{{ c.label }}</span><b>{{ c.fmt(val(sel, c.key)) }}</b>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h2>{{ corrMode === 'corr' ? 'Correlation' : 'Covariance (annualized)' }}</h2>
            <div class="seg">
              <button :class="{ on: corrScope === 'selected' }" @click="corrScope = 'selected'">{{ sel.label }}</button>
              <button :class="{ on: corrScope === 'all' }" @click="corrScope = 'all'">All picks</button>
              <span class="seg-gap"></span>
              <button :class="{ on: corrMode === 'corr' }" @click="corrMode = 'corr'">Corr</button>
              <button :class="{ on: corrMode === 'cov' }" @click="corrMode = 'cov'">Cov</button>
            </div>
          </div>
          <CorrHeatmap :tickers="corr.tickers" :values="corr.values" :mode="corrMode" />
          <p class="card-note foot">daily returns {{ res.base_date }} → {{ res.end_date }}<template v-if="corr.avg != null"> · average pairwise correlation <b>{{ corr.avg.toFixed(2) }}</b></template></p>
        </div>
      </section>

      <p class="method">
        Method: {{ res.method.entry }} · {{ res.method.weighting }} · risk-free {{ res.method.risk_free }} ·
        Sharpe/Sortino on daily excess returns ×252 · annualized return = CAGR (extrapolated for windows under a year) ·
        Calmar = annualized return / |max drawdown| · diversification ratio = Σwσ / σₚ · effective assets = 1/Σw².
        Built {{ res.generated_at?.replace('T', ' ') }} · uploads/pipeline_runs/{{ runId }}/08_performance.json
      </p>
    </template>

    <div v-else-if="!loading" class="empty">
      <div class="empty-icon">◔</div>
      <p>No performance report yet.</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import PipelineStepRail from '../../components/pipeline/PipelineStepRail.vue'
import PerfLineChart from '../../components/pipeline/PerfLineChart.vue'
import CorrHeatmap from '../../components/pipeline/CorrHeatmap.vue'
import { buildPerformance, getPerformance, getRun } from '../../api/pipeline'

const props = defineProps({ runId: { type: String, required: true } })
const router = useRouter()

const run = ref(null)
const res = ref(null)
const stale = ref(false)
const loading = ref(true)
const building = ref(false)
const error = ref('')
const visible = ref(new Set())
const focus = ref(null)
const selected = ref('consensus')
const sortKey = ref('total_return')
const sortDir = ref(-1)
const corrScope = ref('selected')
const corrMode = ref('corr')

// ---- colour: personas take the validated categorical slots in fixed order;
// consensus is primary ink (weight, not a 9th hue); benchmarks are muted + dashed.
const CATEGORICAL = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
const BENCH_DASH = ['7 4', '2 3', '10 3 2 3', '4 2', '1 3', '12 4']
const styles = computed(() => {
  const out = {}
  let pi = 0
  let bi = 0
  for (const p of res.value?.portfolios || []) {
    if (p.kind === 'consensus') out[p.key] = { color: '#0b0b0b', width: 3.2 }
    else if (p.kind === 'persona') out[p.key] = { color: CATEGORICAL[pi++ % 8], width: 1.8 }
    else out[p.key] = { color: bi === 0 ? '#52514e' : '#898781', width: 1.8, dash: BENCH_DASH[bi++ % BENCH_DASH.length] }
  }
  return out
})
const style = (p) => styles.value[p.key] || { color: '#999', width: 2 }

const portfolios = computed(() => res.value?.portfolios || [])
const cons = computed(() => portfolios.value.find(p => p.kind === 'consensus'))
const sel = computed(() => portfolios.value.find(p => p.key === selected.value) || cons.value)
const canBuild = computed(() => run.value?.steps?.consensus?.status === 'completed')
const groups = computed(() => [
  { kind: 'consensus', title: 'CONSENSUS', items: portfolios.value.filter(p => p.kind === 'consensus') },
  { kind: 'persona', title: 'PERSONAS', items: portfolios.value.filter(p => p.kind === 'persona') },
  { kind: 'benchmark', title: 'BENCHMARKS', items: portfolios.value.filter(p => p.kind === 'benchmark') }
])
const versus = computed(() => portfolios.value
  .filter(p => ['SPY', 'universe_ew'].includes(p.key))
  .map(p => ({ key: p.key, label: p.key === 'SPY' ? 'S&P 500' : 'Universe EW', ret: p.metrics.total_return,
               diff: cons.value.metrics.total_return - p.metrics.total_return })))

const chartSeries = (field) => portfolios.value
  .filter(p => visible.value.has(p.key))
  .map(p => ({ key: p.key, label: p.label, values: p[field], ...style(p) }))

const toggle = (key) => {
  const next = new Set(visible.value)
  next.has(key) ? next.delete(key) : next.add(key)
  visible.value = next
  selected.value = key
}
const setGroup = (kind, on) => {
  const next = new Set(visible.value)
  portfolios.value.filter(p => p.kind === kind).forEach(p => (on ? next.add(p.key) : next.delete(p.key)))
  visible.value = next
}

// ---- formatting ----
const pct = (v, digits = 1, sign = true) => v == null ? '—' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
// difference of two returns = percentage points, not percent
const pp = (v, digits = 1) => v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)} pp`
const num = (v, digits = 2) => v == null ? '—' : v.toFixed(digits)

// ---- leaderboard ----
const COLS = [
  { key: 'total_return', label: 'Return', fmt: v => pct(v), better: 1, signed: true, help: 'Total return since entry' },
  { key: 'annualized_return', label: 'Ann. return', fmt: v => pct(v), better: 1, signed: true, help: 'CAGR (extrapolated for < 1 year)' },
  { key: 'volatility', label: 'Volatility', fmt: v => pct(v, 1, false), better: -1, help: 'Annualized std of daily returns' },
  { key: 'sharpe', label: 'Sharpe', fmt: v => num(v), better: 1, signed: true, help: 'Excess return / volatility' },
  { key: 'sortino', label: 'Sortino', fmt: v => num(v), better: 1, signed: true, help: 'Excess return / downside deviation' },
  { key: 'max_drawdown', label: 'Max DD', fmt: v => pct(v), better: 1, help: 'Worst peak-to-trough fall' },
  { key: 'calmar', label: 'Calmar', fmt: v => num(v), better: 1, signed: true, help: 'Annualized return / |max drawdown|' },
  { key: 'diversification_ratio', label: 'Div. ratio', fmt: v => num(v), better: 1, help: 'Σwσ / σp — 1 = no diversification benefit' },
  { key: 'effective_assets', label: 'Eff. assets', fmt: v => num(v, 1), better: 1, help: '1 / Σw² — effective number of holdings' },
  { key: 'h4w', label: '4 weeks', fmt: v => pct(v), better: 1, signed: true, help: 'Return at the first trading day ≥ 28 days after entry' },
  { key: 'h6w', label: '6 weeks', fmt: v => pct(v), better: 1, signed: true, help: 'Return at the first trading day ≥ 42 days after entry' }
]
const val = (p, key) => {
  if (!p) return null
  if (key === 'h4w') return p.horizons['4w']?.return ?? null
  if (key === 'h6w') return p.horizons['6w']?.return ?? null
  return p.metrics[key]
}
const sortBy = (key) => {
  if (sortKey.value === key) sortDir.value = -sortDir.value
  else { sortKey.value = key; sortDir.value = COLS.find(c => c.key === key).better === -1 ? 1 : -1 }
}
const sortedRows = computed(() => [...portfolios.value].sort((a, b) => {
  const va = val(a, sortKey.value)
  const vb = val(b, sortKey.value)
  if (va == null) return 1
  if (vb == null) return -1
  return (va - vb) * sortDir.value
}))
// best per column among consensus + personas (benchmarks are the yardstick, not contestants)
const best = computed(() => {
  const out = {}
  const pool = portfolios.value.filter(p => p.kind !== 'benchmark')
  for (const c of COLS) {
    let top = null
    for (const p of pool) {
      const v = val(p, c.key)
      if (v == null) continue
      if (top === null || (v - val(top, c.key)) * c.better > 0) top = p
    }
    out[c.key] = top?.key
  }
  return out
})
const maxAbsRet = computed(() => Math.max(0.0001, ...portfolios.value.map(p => Math.abs(p.metrics.total_return || 0))))
const retBar = (v) => {
  const w = Math.abs(v || 0) / maxAbsRet.value * 50
  return v >= 0 ? { left: '50%', width: w + '%', background: '#0ca30c' } : { left: 50 - w + '%', width: w + '%', background: '#d03b3b' }
}
const maxAbsContrib = computed(() => Math.max(0.0001, ...(sel.value?.holdings || []).map(h => Math.abs(h.contribution || 0))))
const contribBar = (v) => {
  const w = Math.abs(v || 0) / maxAbsContrib.value * 50
  return v >= 0 ? { left: '50%', width: w + '%' } : { left: 50 - w + '%', width: w + '%' }
}

// ---- correlation ----
const corr = computed(() => {
  const c = res.value?.correlation
  if (!c) return { tickers: [], values: [], avg: null }
  const wanted = corrScope.value === 'all' ? c.tickers : c.tickers.filter(t => sel.value?.tickers.includes(t))
  const idx = wanted.map(t => c.tickers.indexOf(t))
  const src = corrMode.value === 'corr' ? c.corr : c.cov
  const values = idx.map(i => idx.map(j => src[i][j]))
  const pairs = []
  idx.forEach((i, a) => idx.forEach((j, b) => { if (a < b && c.corr[i][j] != null) pairs.push(c.corr[i][j]) }))
  return { tickers: wanted.length > 1 ? wanted : [], values, avg: pairs.length ? pairs.reduce((s, v) => s + v, 0) / pairs.length : null }
})

// ---- data ----
const load = async () => {
  try {
    const [r, p] = await Promise.all([getRun(props.runId), getPerformance(props.runId)])
    run.value = r.data
    res.value = p.data.result
    stale.value = p.data.stale
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
const build = async () => {
  building.value = true
  error.value = ''
  try {
    const p = (await buildPerformance(props.runId)).data
    res.value = p.result
    stale.value = p.stale
    run.value = (await getRun(props.runId)).data
  } catch (e) {
    error.value = e.message
  } finally {
    building.value = false
  }
}

watch(res, (r) => {
  if (!r) return
  visible.value = new Set(r.portfolios.map(p => p.key))   // everything on; legend toggles
  if (!r.portfolios.some(p => p.key === selected.value)) selected.value = 'consensus'
})

onMounted(load)
</script>

<style scoped>
.perf {
  min-height: 100vh;
  background: #F9F9F7;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
  color: #0B0B0B;
  padding-bottom: 48px;
}
code, .mono { font-family: 'JetBrains Mono', monospace; }

.navbar { height: 52px; background: #000; color: #FFF; display: flex; align-items: center; justify-content: space-between; padding: 0 28px; position: sticky; top: 0; z-index: 20; }
.nav-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.nav-brand { font-family: 'JetBrains Mono', monospace; font-weight: 800; letter-spacing: 1px; font-size: 1.1rem; cursor: pointer; }
.nav-tag { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; background: #FF5722; padding: 3px 8px; letter-spacing: 1px; }
.crumb { font-size: 12px; color: #999; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.crumb b { color: #FFF; font-weight: 600; }
.nav-right { display: flex; gap: 20px; }
.nav-link { font-family: 'JetBrains Mono', monospace; font-size: 12px; opacity: 0.75; cursor: pointer; }
.nav-link:hover { opacity: 1; }

.head { display: grid; grid-template-columns: 1fr 560px; gap: 32px; align-items: end; padding: 22px 28px 16px; max-width: 1480px; margin: 0 auto; }
.eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 700; color: #999; letter-spacing: 1.5px; display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.orange-sq { width: 8px; height: 8px; background: #FF5722; display: inline-block; }
.title { font-size: 2.2rem; font-weight: 600; letter-spacing: -1.2px; line-height: 1.05; margin-bottom: 8px; }
.accent { background: linear-gradient(90deg, #000 0%, #FF5722 100%); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.sub { font-size: 12.5px; color: #52514E; line-height: 1.6; }
.head-side { display: flex; flex-direction: column; gap: 12px; }
.controls { display: flex; justify-content: flex-end; align-items: center; gap: 12px; }
.updated { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #898781; }
.main-btn { display: flex; align-items: center; gap: 8px; background: #000; color: #FFF; border: none; border-radius: 6px; padding: 11px 18px; font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 12px; letter-spacing: 0.8px; cursor: pointer; transition: all 0.2s; }
.main-btn:hover:not(:disabled) { background: #FF5722; box-shadow: 0 6px 18px rgba(255,87,34,0.3); }
.main-btn:disabled { background: #BBB; cursor: not-allowed; }
.spin { width: 11px; height: 11px; border: 2px solid rgba(255,255,255,0.35); border-top-color: #FFF; border-radius: 50%; animation: spin 0.7s linear infinite; }

.banner { max-width: 1424px; margin: 0 auto 10px; padding: 9px 12px; border-radius: 6px; font-size: 12px; }
.banner.error { background: #FFEBEE; color: #C62828; border-left: 3px solid #d03b3b; }
.banner.warn { background: #FFF8E1; color: #7A5200; border-left: 3px solid #fab219; }
.banner.note { background: #F1F0EC; color: #52514E; border-left: 3px solid #C3C2B7; }

.kpis, .legend, .card, .split, .method, .empty { max-width: 1424px; margin-left: auto; margin-right: auto; }
.kpis { display: grid; grid-template-columns: 1.4fr repeat(4, 1fr); gap: 10px; margin-bottom: 14px; padding: 0 28px; box-sizing: content-box; }
.kpi { background: #FFF; border: 1px solid rgba(11,11,11,0.08); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 3px; }
.kpi.hero { background: #0B0B0B; color: #FFF; border-color: #0B0B0B; }
.kpi-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.6px; color: #898781; font-weight: 600; }
.kpi.hero .kpi-label { color: #FF8A65; }
.kpi-value { font-size: 26px; font-weight: 600; letter-spacing: -0.5px; }
.kpi.hero .kpi-value { font-size: 36px; }
.kpi-value small { font-size: 15px; color: #898781; font-weight: 500; }
.kpi-value.good { color: #006300; }
.kpi-value.bad { color: #d03b3b; }
.icon { font-size: 15px; margin-right: 4px; vertical-align: 3px; }
.kpi-sub { font-size: 11px; color: #898781; font-family: 'JetBrains Mono', monospace; }
.kpi.hero .kpi-sub { color: #AAA; }

.legend { display: flex; flex-wrap: wrap; gap: 8px 22px; padding: 0 28px 12px; box-sizing: content-box; }
.lg-group { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
.lg-title { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 800; color: #898781; letter-spacing: 1px; margin-right: 3px; }
.lg-chip { display: flex; align-items: center; gap: 6px; border: 1px solid rgba(11,11,11,0.1); background: #FFF; border-radius: 14px; padding: 4px 10px 4px 7px; font-size: 11.5px; color: #0B0B0B; cursor: pointer; transition: all 0.15s; }
.lg-chip:hover { border-color: #0B0B0B; }
.lg-chip.off { opacity: 0.4; background: transparent; }
.lg-chip.sel { box-shadow: 0 0 0 1.5px #FF5722; }
.lg-mini { border: none; background: none; font-size: 10.5px; color: #FF5722; cursor: pointer; font-weight: 600; }

.card { background: #FFF; border: 1px solid rgba(11,11,11,0.08); border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; box-sizing: border-box; width: calc(100% - 56px); max-width: 1424px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
.card-head h2 { font-size: 15px; font-weight: 600; letter-spacing: -0.2px; }
.card-note { font-size: 11px; color: #898781; }
.card-note.foot { margin-top: 8px; }
.sub-head { margin-top: 18px; }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #898781; font-weight: 700; text-align: right; padding: 8px 10px; border-bottom: 1px solid #E1E0D9; cursor: pointer; white-space: nowrap; user-select: none; }
th.sorted { color: #0B0B0B; }
th.l, td.l { text-align: left; }
td { text-align: right; padding: 9px 10px; border-bottom: 1px solid #F1F0EC; font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace; font-size: 11.5px; white-space: nowrap; position: relative; }
tbody tr { cursor: pointer; transition: background 0.12s; }
tbody tr:hover { background: #FAFAF8; }
tbody tr.sel { background: #FFF4EF; }
tbody tr.consensus td { font-weight: 700; }
tbody tr.benchmark td { color: #52514E; }
td.best { font-weight: 800; background: rgba(12,163,12,0.07); }
td.neg { color: #B3261E; }
.row-sw { display: inline-block; width: 12px; height: 4px; border-radius: 2px; margin-right: 8px; vertical-align: middle; }
.row-name { font-family: 'Space Grotesk', system-ui, sans-serif; font-size: 12.5px; font-weight: 600; }
.kind { font-size: 9.5px; color: #898781; margin-left: 8px; font-weight: 500; }
.ret-bar { position: absolute; left: 10px; right: 10px; bottom: 4px; height: 3px; background: #F1F0EC; border-radius: 2px; overflow: hidden; }
.ret-bar span { position: absolute; top: 0; bottom: 0; border-radius: 2px; }

.split { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; width: calc(100% - 56px); }
.split .card { width: 100%; margin: 0; }
.holdings { display: flex; flex-direction: column; gap: 2px; max-height: 330px; overflow-y: auto; }
.h-row { display: grid; grid-template-columns: 56px 1fr 90px 1.3fr; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid #F4F3EF; font-size: 11.5px; }
.h-tk { font-family: 'JetBrains Mono', monospace; font-weight: 800; }
.h-w { display: flex; align-items: center; gap: 8px; font-family: 'JetBrains Mono', monospace; color: #52514E; }
.wbar { flex: 1; height: 6px; background: #F1F0EC; border-radius: 3px; overflow: hidden; }
.wbar span { display: block; height: 100%; background: #0B0B0B; border-radius: 3px; }
.h-r { font-family: 'JetBrains Mono', monospace; font-weight: 700; text-align: right; }
.good { color: #006300; }
.bad { color: #B3261E; }
.h-c { display: flex; align-items: center; gap: 8px; font-family: 'JetBrains Mono', monospace; color: #52514E; justify-content: flex-end; }
.cbar { position: relative; flex: 1; height: 8px; }
.cbar .mid { position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: #C3C2B7; }
.cbar .fill { position: absolute; top: 0; bottom: 0; border-radius: 4px; }
.cbar .fill.pos { background: #0ca30c; }
.cbar .fill.neg { background: #d03b3b; }
.metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 14px; }
.mg { background: #F9F9F7; border-radius: 6px; padding: 7px 9px; display: flex; justify-content: space-between; font-size: 10.5px; color: #898781; }
.mg b { color: #0B0B0B; font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }
.seg { display: flex; background: #F4F3EF; padding: 3px; border-radius: 6px; gap: 2px; align-items: center; }
.seg button { border: none; background: none; padding: 4px 9px; border-radius: 4px; font-size: 11px; font-weight: 600; color: #777; cursor: pointer; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.seg-gap { width: 1px; height: 14px; background: #D5D4CE; margin: 0 3px; }

.method { font-size: 10.5px; color: #898781; line-height: 1.7; padding: 4px 28px 0; box-sizing: content-box; }
.empty { text-align: center; padding: 80px 0; color: #898781; }
.empty-icon { font-size: 40px; color: #D5D4CE; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 1150px) {
  .head { grid-template-columns: 1fr; }
  .kpis { grid-template-columns: repeat(2, 1fr); }
  .split { grid-template-columns: 1fr; }
}
</style>
