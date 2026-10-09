<template>
  <section class="flab" :class="`lens-${lens}`">
    <!-- ============ header ============ -->
    <div class="flab-head">
      <div class="flab-title">
        <span class="block-num">F</span>
        <div>
          <h2>Fundamental Lab</h2>
          <p>Financial ratios per company, benchmarked against <b>every</b> S&amp;P 500 company in the same industry
            — min, median, average, max — then ranked.</p>
        </div>
      </div>
      <div class="flab-status">
        <span v-if="state === 'done'" class="pill ok">● {{ result.total - Object.keys(result.failed || {}).length }}/{{ result.total }} companies · as_of {{ result.as_of_date }}</span>
        <span v-else-if="state === 'running'" class="pill run"><span class="spin"></span> {{ progress.processed }}/{{ progress.total || '…' }}</span>
        <span v-else-if="!screenReady" class="pill idle">waiting for the screen</span>
      </div>
    </div>

    <!-- ============ lens picker (always visible) ============ -->
    <div class="lens-row">
      <button v-for="key in LENS_KEYS" :key="key" class="lens-card" :class="[`lc-${key}`, { on: lens === key }]"
              @click="lens = key">
        <span class="lens-icon">
          <svg v-if="key === 'value'" viewBox="0 0 24 24"><path d="M4 18h16M6 18V9m4 9V6m4 12v-5m4 5V11" /></svg>
          <svg v-else viewBox="0 0 24 24"><path d="M4 17l5-5 4 3 7-8M15 7h5v5" /></svg>
        </span>
        <span class="lens-text">
          <span class="lens-name">{{ catalog.lenses?.[key]?.label || key }}</span>
          <span class="lens-tag">{{ catalog.lenses?.[key]?.tagline }}</span>
        </span>
        <span class="lens-q">{{ catalog.lenses?.[key]?.question }}</span>
      </button>
    </div>

    <!-- ============ not loaded yet ============ -->
    <div v-if="state === 'idle' || state === 'failed'" class="intro">
      <div class="intro-grid">
        <div class="intro-step"><span>01</span><b>Statements</b>Last 4 quarters + last full fiscal year from yfinance, only periods published before <code>as_of</code> (quarter +{{ catalog.rules?.quarter_lag_days }}d, year +{{ catalog.rules?.annual_lag_days }}d).</div>
        <div class="intro-step"><span>02</span><b>Ratios</b>A value set and a growth set per sector — banks, REITs and utilities use their own ratios.</div>
        <div class="intro-step"><span>03</span><b>Benchmark</b>Min · median · average · max of each ratio across the whole industry (all tiers), outliers removed.</div>
        <div class="intro-step"><span>04</span><b>Rank</b>Where each company sits: percentile, above / below the median, industry min / max.</div>
      </div>
      <div class="intro-cta">
        <button class="load-btn" :disabled="!screenReady || starting" @click="load">
          <span v-if="starting" class="spin light"></span>
          LOAD FUNDAMENTALS · {{ peerCount }} COMPANIES →
        </button>
        <span class="intro-note" v-if="!screenReady">Finish the universe screen first.</span>
        <span class="intro-note" v-else>≈ {{ Math.max(1, Math.round(peerCount / 4 / 60)) }} min the first time today, instant after (cached per day).</span>
      </div>
      <div v-if="error" class="msg error">{{ error }}</div>
    </div>

    <!-- ============ loading ============ -->
    <div v-else-if="state === 'running'" class="loading">
      <div class="load-track"><div class="load-fill" :style="{ width: progressPct + '%' }"></div></div>
      <div class="load-text">Reading financial statements · <b>{{ progress.current || '…' }}</b></div>
      <div class="ticker-rain">
        <span v-for="t in rainTickers" :key="t" :class="{ done: rainDone.has(t) }">{{ t }}</span>
      </div>
    </div>

    <!-- ============ loaded ============ -->
    <template v-else-if="state === 'done'">
      <div class="controls">
        <div class="ctl">
          <span class="ctl-label">Financials</span>
          <div class="seg">
            <button v-for="b in ['ttm', 'fy']" :key="b" :class="{ on: basis === b }" @click="basis = b">
              {{ b === 'ttm' ? 'Last 4 quarters' : 'Last fiscal year' }}
              <i>{{ result.coverage[b] }}/{{ result.total }}</i>
            </button>
          </div>
        </div>
        <div class="ctl">
          <span class="ctl-label">Benchmark against</span>
          <div class="seg">
            <button :class="{ on: level === 'sub_industry' }" @click="level = 'sub_industry'">Industry</button>
            <button :class="{ on: level === 'sector' }" @click="level = 'sector'">Whole sector</button>
          </div>
        </div>
        <div class="ctl">
          <span class="ctl-label">Passes when it beats the median on</span>
          <div class="seg">
            <button v-for="t in catalog.thresholds" :key="t" :class="{ on: threshold === t }" @click="threshold = t">
              {{ t === 1 ? 'all' : `≥ ${Math.round(t * 100)}%` }}
            </button>
          </div>
        </div>
        <button class="formula-btn" :class="{ on: showFormulas }" @click="showFormulas = !showFormulas">ƒ Formulas</button>
      </div>
      <div v-if="basisWarning" class="msg warn">{{ basisWarning }}</div>

      <!-- sector tabs -->
      <div class="sector-tabs" v-if="sectors.length > 1">
        <button v-for="s in sectors" :key="s" :class="{ on: activeSector === s }" @click="activeSector = s">
          {{ s }} <i>{{ candidatesIn(s).length }}</i>
        </button>
      </div>

      <!-- metric chips -->
      <div class="metric-bar">
        <span class="mb-label">{{ catalog.lenses[lens].label }} ratios · {{ activeSector }}</span>
        <button v-for="m in profileMetrics" :key="m" class="mchip" :class="{ off: isDisabled(m) }"
                :title="`${M(m).name} = ${M(m).formula}`" @click="toggleMetric(m)">
          <span class="mchip-dir" :title="M(m).better === 'lower' ? 'lower is better' : 'higher is better'">{{ M(m).better === 'lower' ? '↓' : '↑' }}</span>
          {{ M(m).label }}
        </button>
        <span class="mb-note">{{ profile.note }}</span>
      </div>

      <!-- formula sheet -->
      <transition name="fold">
        <div v-if="showFormulas" class="formulas">
          <div v-for="lk in ['value', 'growth']" :key="lk" class="f-col">
            <div class="f-head" :class="`fh-${lk}`">{{ catalog.lenses[lk].label }} · {{ activeSector }}</div>
            <div v-for="m in catalog.profiles[activeSector]?.[lk] || []" :key="m" class="f-row">
              <span class="f-label">{{ M(m).label }}</span>
              <span class="f-formula">{{ M(m).formula }}</span>
              <span class="f-dir">{{ M(m).better === 'lower' ? '↓ lower' : '↑ higher' }} is better</span>
              <span class="f-nm" v-if="M(m).nm">N/M when {{ M(m).nm }}</span>
            </div>
          </div>
          <div class="f-foot">
            <span><b>Last 4 quarters:</b> flows = sum of the 4 latest quarters, balance sheet = latest quarter; {{ catalog.growth_basis_note?.ttm }}.</span>
            <span><b>Last fiscal year:</b> {{ catalog.growth_basis_note?.fy }}.</span>
            <span><b>Price:</b> close on or before as_of; market cap = price × latest share count.</span>
            <span><b>Statistics:</b> N/M values are left out; {{ catalog.rules?.outlier }}.</span>
          </div>
        </div>
      </transition>

      <!-- ============ industry benchmark ============ -->
      <div class="bench">
        <div class="bench-head">
          <div>
            <div class="eyebrow">INDUSTRY BENCHMARK</div>
            <div class="bench-group">
              <button v-for="g in groupsInSector" :key="g.name" class="gchip" :class="{ on: focusGroup === g.name }"
                      @click="focusGroup = g.name">
                {{ g.name }} <i>{{ g.peers }} peer{{ g.peers === 1 ? '' : 's' }} · {{ g.candidates }} yours</i>
              </button>
            </div>
          </div>
          <div class="bench-legend">
            <span><i class="lg-box"></i> middle 50% (Q1–Q3)</span>
            <span><i class="lg-med"></i> median</span>
            <span><i class="lg-mean"></i> average</span>
            <span><i class="lg-dot peer"></i> peer</span>
            <span><i class="lg-dot mine"></i> your company</span>
          </div>
        </div>
        <div v-if="groupInfo?.small" class="msg warn">
          Only {{ groupInfo.peers }} S&amp;P 500 companies in “{{ focusGroup }}” — statistics are fragile. Try “Whole sector”.
        </div>
        <div class="bench-grid">
          <div v-for="m in activeMetrics" :key="m" class="bcard">
            <div class="bcard-top">
              <span class="bcard-label">{{ M(m).label }}</span>
              <span class="bcard-name">{{ M(m).name }}</span>
              <span class="bcard-n">n={{ gstat(m)?.n || 0 }}<template v-if="gstat(m)?.nm"> · {{ gstat(m).nm }} N/M</template><template v-if="gstat(m)?.outliers?.length"> · {{ gstat(m).outliers.length }} outlier</template></span>
            </div>
            <template v-if="gstat(m)?.n">
              <svg class="strip" viewBox="0 0 320 64" preserveAspectRatio="none">
                <line x1="10" x2="310" y1="34" y2="34" class="axis" />
                <rect :x="Math.min(sx(m, gstat(m).q1), sx(m, gstat(m).q3))" y="22"
                      :width="Math.max(2, Math.abs(sx(m, gstat(m).q3) - sx(m, gstat(m).q1)))" height="24" rx="3" class="iqr" />
                <line :x1="sx(m, gstat(m).median)" :x2="sx(m, gstat(m).median)" y1="16" y2="52" class="median" />
                <path :d="diamond(sx(m, gstat(m).mean), 34)" class="mean" />
                <circle v-for="([t, v]) in gstat(m).values" :key="t" :cx="sx(m, v)" :cy="34 + jitter(t)" r="2.6"
                        class="peer" :class="{ hidden: isCandidate(t) }"><title>{{ t }} · {{ fmt(m, v) }}</title></circle>
                <g v-for="c in groupCandidates" :key="c.ticker">
                  <template v-if="val(c.ticker, m) != null">
                    <circle :cx="sx(m, val(c.ticker, m))" cy="34" :r="focusTicker === c.ticker ? 7 : 5"
                            class="mine" :class="{ off: !c.selected, focus: focusTicker === c.ticker }"
                            @mouseenter="focusTicker = c.ticker">
                      <title>{{ c.ticker }} · {{ fmt(m, val(c.ticker, m)) }} · {{ pctText(c.ticker, m) }}</title>
                    </circle>
                    <text v-if="focusTicker === c.ticker" :x="sx(m, val(c.ticker, m))" y="11" class="dot-label">{{ c.ticker }}</text>
                  </template>
                </g>
                <text x="10" y="62" class="end-label">worse</text>
                <text x="310" y="62" class="end-label" text-anchor="end">better</text>
              </svg>
              <div class="bstats">
                <div><span>min</span><b>{{ fmt(m, gstat(m).min) }}</b></div>
                <div><span>median</span><b>{{ fmt(m, gstat(m).median) }}</b></div>
                <div><span>average</span><b>{{ fmt(m, gstat(m).mean) }}</b></div>
                <div><span>max</span><b>{{ fmt(m, gstat(m).max) }}</b></div>
              </div>
              <div class="bfocus" v-if="focusTicker && candidateGroup(focusTicker) === focusGroup">
                <b>{{ focusTicker }}</b>&nbsp;
                <template v-if="val(focusTicker, m) != null">
                  {{ fmt(m, val(focusTicker, m)) }} ·
                  <span :class="rank(focusTicker, m)?.side">{{ sideText(rank(focusTicker, m)) }}</span>
                  · {{ pctText(focusTicker, m) }}
                </template>
                <span v-else class="nm">{{ flagText(focusTicker, m) }}</span>
              </div>
            </template>
            <div v-else class="bempty">No company in this industry has a meaningful {{ M(m).label }}.</div>
          </div>
        </div>
      </div>

      <!-- ============ ranking ============ -->
      <div class="rank">
        <div class="rank-head">
          <div>
            <div class="eyebrow">RANKING · {{ activeSector.toUpperCase() }}</div>
            <div class="rank-sub">
              <b>{{ passingInSector.length }}</b> of {{ sectorRows.length }} companies beat their industry median on
              {{ threshold === 1 ? 'every' : `at least ${Math.round(threshold * 100)}% of the` }} active {{ catalog.lenses[lens].label.toLowerCase() }} ratios.
            </div>
          </div>
          <div class="rank-actions">
            <div class="seg small">
              <button v-for="v in ['all', 'passing', 'selected']" :key="v" :class="{ on: show === v }" @click="show = v">{{ v }}</button>
            </div>
            <button class="act primary" @click="selectPassing" :title="'Keep only companies that pass, in every sector'">
              ✓ SELECT PASSING <i>{{ passingAll.length }}</i>
            </button>
            <button class="act" @click="selectAll">Select all</button>
            <button class="act" @click="clearAll">Clear</button>
          </div>
        </div>

        <div class="rtable" :style="{ '--cols': activeMetrics.length }">
          <div class="r-head">
            <span></span>
            <span class="sortable" :class="{ on: sortKey === 'ticker' }" @click="sortBy('ticker')">Company</span>
            <span v-for="m in activeMetrics" :key="m" class="sortable r" :class="{ on: sortKey === m }" @click="sortBy(m)"
                  :title="M(m).formula">
              {{ M(m).label }} <em>{{ M(m).better === 'lower' ? '↓' : '↑' }}</em>
            </span>
            <span class="sortable c" :class="{ on: sortKey === 'beats' }" @click="sortBy('beats')">Beats</span>
            <span class="sortable c" :class="{ on: sortKey === 'score' }" @click="sortBy('score')">Score</span>
          </div>
          <div v-for="row in tableRows" :key="row.ticker" class="r-row"
               :class="{ sel: row.selected, focus: focusTicker === row.ticker, pass: row.ev.passes }"
               @click="focusRow(row)">
            <span class="r-check" @click.stop="$emit('toggle', row.ticker)">
              <span class="check" :class="{ on: row.selected }">✓</span>
            </span>
            <span class="r-company">
              <b>{{ row.ticker }}</b>
              <span class="r-name">{{ row.company_name }}</span>
              <span class="r-ind">{{ row.groupName }}<template v-if="row.tier"> · {{ row.tier }}</template></span>
            </span>
            <span v-for="m in activeMetrics" :key="m" class="mcell" :class="rank(row.ticker, m)?.side || 'none'"
                  :title="cellTitle(row.ticker, m)">
              <template v-if="val(row.ticker, m) != null">
                <span class="mval">{{ fmt(m, val(row.ticker, m)) }}<sup v-if="rank(row.ticker, m)?.extreme" :class="rank(row.ticker, m).extreme">{{ rank(row.ticker, m).extreme.toUpperCase() }}</sup></span>
                <span class="mbar"><i :style="{ width: (rank(row.ticker, m)?.pct ?? 0) + '%' }"></i></span>
              </template>
              <span v-else class="mna">{{ flag(row.ticker, m) === 'nm' ? 'N/M' : '—' }}</span>
            </span>
            <span class="r-beats c">
              <span class="beats" :class="{ ok: row.ev.passes }">{{ row.ev.beat_count }}/{{ row.ev.n }}</span>
            </span>
            <span class="r-score c">
              <svg viewBox="0 0 36 36" class="ring">
                <circle cx="18" cy="18" r="15" class="ring-bg" />
                <circle v-if="row.ev.score != null" cx="18" cy="18" r="15" class="ring-fg"
                        :stroke-dasharray="`${row.ev.score * 0.9425} 94.25`" />
                <text x="18" y="22" text-anchor="middle">{{ row.ev.score != null ? Math.round(row.ev.score) : '–' }}</text>
              </svg>
            </span>
          </div>
          <div v-if="!tableRows.length" class="r-empty">No companies in this view.</div>
        </div>
        <p class="rank-foot">
          Cells: value, then a bar for the industry percentile (full = best in the industry, direction-adjusted).
          <span class="better-t">Green</span> = better than the median, <span class="worse-t">red</span> = worse.
          MIN / MAX = the industry's lowest / highest value. Score = average percentile across the active ratios (N/M counts as 0; no score with data on fewer than half).
          Financial periods used: {{ periodText }}.
        </p>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import { startFundamentals, getFundamentals } from '../../api/pipeline'

const props = defineProps({
  catalog: { type: Object, default: () => ({ lenses: {}, metrics: {}, profiles: {}, thresholds: [0.5] }) },
  screenTaskId: { type: String, default: null },
  screenReady: { type: Boolean, default: false },
  peerCount: { type: Number, default: 0 },
  // rows the user can keep: {ticker, company_name, gics_sector, gics_sub_industry, tier, state: 'in' | 'excluded'}
  candidates: { type: Array, default: () => [] }
})
const emit = defineEmits(['toggle', 'set-excluded', 'config'])

const LENS_KEYS = ['value', 'growth']   // fixed order (JSON key order is not)
const state = ref('idle')          // idle | running | done | failed
const starting = ref(false)
const error = ref('')
const taskId = ref(null)
const result = ref(null)
const progress = ref({ processed: 0, total: 0, current: '' })
const rainDone = ref(new Set())
let timer = null

const lens = ref('value')
const basis = ref('ttm')
const level = ref('sub_industry')
const threshold = ref(0.5)
const disabled = ref({})           // sector -> [metric keys switched off]
const showFormulas = ref(false)
const activeSector = ref('')
const focusGroup = ref('')
const focusTicker = ref('')
const sortKey = ref('score')
const sortDir = ref(-1)
const show = ref('all')

const M = (m) => props.catalog.metrics?.[m] || { label: m, name: m, formula: '', better: 'higher', unit: 'x' }

// ---------------------------------------------------------------- loading
const stop = () => { if (timer) { clearInterval(timer); timer = null } }
const reset = () => {
  stop()
  state.value = 'idle'
  result.value = null
  taskId.value = null
  error.value = ''
  rainDone.value = new Set()
  emit('config', null)
}
watch(() => props.screenTaskId, reset)

const progressPct = computed(() => progress.value.total ? Math.round(progress.value.processed * 100 / progress.value.total) : 2)
const rainTickers = computed(() => props.candidates.map(c => c.ticker).slice(0, 60))

const poll = async () => {
  try {
    const { data: task } = await getFundamentals(taskId.value)
    const d = task.progress_detail || {}
    progress.value = { processed: d.processed || 0, total: d.total || 0, current: d.current || '' }
    if (d.current) rainDone.value = new Set([...rainDone.value, d.current])
    if (task.status === 'completed') {
      stop()
      result.value = task.result
      const r = task.result
      basis.value = r.coverage.ttm >= 0.8 * r.total ? 'ttm' : 'fy'
      state.value = 'done'
    } else if (task.status === 'failed') {
      stop()
      state.value = 'failed'
      error.value = task.error
    }
  } catch (e) {
    stop()
    state.value = 'failed'
    error.value = e.message
  }
}

const load = async () => {
  starting.value = true
  error.value = ''
  try {
    const res = await startFundamentals(props.screenTaskId)
    taskId.value = res.data.task_id
    state.value = 'running'
    progress.value = { processed: 0, total: props.peerCount, current: '' }
    timer = setInterval(poll, 900)
    poll()
  } catch (e) {
    state.value = 'failed'
    error.value = e.message
  } finally {
    starting.value = false
  }
}
onUnmounted(stop)

// ------------------------------------------------------------ derived data
const T = (t) => result.value?.tickers?.[t]
const val = (t, m) => T(t)?.[basis.value]?.values?.[m] ?? null
const flag = (t, m) => T(t)?.[basis.value]?.flags?.[m] || 'na'
const rank = (t, m) => result.value?.ranks?.[basis.value]?.[level.value]?.[t]?.[m] || null
const groupKey = (t) => level.value === 'sector' ? T(t)?.sector : T(t)?.sub_industry
const candidateGroup = groupKey

const known = computed(() => props.candidates.filter(c => T(c.ticker)))
const sectors = computed(() => [...new Set(known.value.map(c => c.gics_sector))].sort())
const candidatesIn = (s) => known.value.filter(c => c.gics_sector === s)
watch(sectors, (list) => { if (!list.includes(activeSector.value)) activeSector.value = list[0] || '' }, { immediate: true })

const profileFor = (s) => props.catalog.profiles?.[s] || { value: [], growth: [], note: '' }
const profile = computed(() => profileFor(activeSector.value))
const profileMetrics = computed(() => profile.value[lens.value] || [])
const isDisabled = (m, s = activeSector.value) => (disabled.value[s] || []).includes(m)
const activeMetricsFor = (s) => (profileFor(s)[lens.value] || []).filter(m => !isDisabled(m, s))
const activeMetrics = computed(() => activeMetricsFor(activeSector.value))
const toggleMetric = (m) => {
  const s = activeSector.value
  const cur = new Set(disabled.value[s] || [])
  if (cur.has(m)) cur.delete(m)
  else if (activeMetrics.value.length > 1) cur.add(m)       // keep at least one ratio
  disabled.value = { ...disabled.value, [s]: [...cur] }
}

// same rule as backend fundamentals.evaluate
const evaluate = (t) => {
  const metrics = activeMetricsFor(T(t)?.sector)
  const beats = metrics.filter(m => rank(t, m)?.side === 'better')
  // N/M counts as the bottom percentile, missing data is skipped; < half the ratios -> no score
  const pcts = metrics.filter(m => rank(t, m) || flag(t, m) === 'nm').map(m => rank(t, m)?.pct ?? 0)
  return {
    n: metrics.length,
    beat_count: beats.length,
    coverage: pcts.length,
    score: pcts.length && pcts.length * 2 >= metrics.length ? pcts.reduce((a, b) => a + b, 0) / pcts.length : null,
    passes: metrics.length > 0 && beats.length >= Math.ceil(threshold.value * metrics.length - 1e-9)
  }
}
const evals = computed(() => {
  const out = {}
  known.value.forEach(c => { out[c.ticker] = evaluate(c.ticker) })
  return out
})
const passingAll = computed(() => known.value.filter(c => evals.value[c.ticker]?.passes).map(c => c.ticker))

const sectorRows = computed(() => candidatesIn(activeSector.value).map(c => ({
  ...c,
  selected: c.state === 'in',
  groupName: groupKey(c.ticker),
  ev: evals.value[c.ticker]
})))
const passingInSector = computed(() => sectorRows.value.filter(r => r.ev.passes))

const sortBy = (k) => {
  if (sortKey.value === k) sortDir.value *= -1
  else { sortKey.value = k; sortDir.value = k === 'ticker' ? 1 : -1 }
}
const tableRows = computed(() => {
  let rows = sectorRows.value
  if (show.value === 'passing') rows = rows.filter(r => r.ev.passes)
  if (show.value === 'selected') rows = rows.filter(r => r.selected)
  const k = sortKey.value
  const key = (r) => k === 'ticker' ? r.ticker
    : k === 'score' ? (r.ev.score ?? -1)
      : k === 'beats' ? r.ev.beat_count + (r.ev.score ?? 0) / 1000
        : (rank(r.ticker, k)?.pct ?? -1)
  return [...rows].sort((a, b) => {
    const x = key(a)
    const y = key(b)
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir.value
  })
})

// groups of the active sector that contain at least one candidate
const groupsInSector = computed(() => {
  const g = {}
  candidatesIn(activeSector.value).forEach(c => {
    const name = groupKey(c.ticker)
    g[name] = (g[name] || 0) + 1
  })
  const bench = result.value?.benchmarks?.[basis.value]?.[level.value] || {}
  return Object.entries(g).map(([name, n]) => ({ name, candidates: n, peers: bench[name]?.peers || 0 }))
    .sort((a, b) => b.candidates - a.candidates || a.name.localeCompare(b.name))
})
watch(groupsInSector, (list) => {
  if (!list.find(g => g.name === focusGroup.value)) focusGroup.value = list[0]?.name || ''
}, { immediate: true })
const groupInfo = computed(() => result.value?.benchmarks?.[basis.value]?.[level.value]?.[focusGroup.value])
const gstat = (m) => groupInfo.value?.metrics?.[m]
const groupCandidates = computed(() => sectorRows.value.filter(r => r.groupName === focusGroup.value))
const candidateSet = computed(() => new Set(groupCandidates.value.map(c => c.ticker)))
const isCandidate = (t) => candidateSet.value.has(t)

const focusRow = (row) => {
  focusTicker.value = focusTicker.value === row.ticker ? '' : row.ticker
  if (focusTicker.value) focusGroup.value = row.groupName
}

// ------------------------------------------------------------- strip plot
// x scale: clean peer range + your companies (outliers included), "better" always on the right
const domain = (m) => {
  const s = gstat(m)
  const vs = [s.min, s.max, ...groupCandidates.value.map(c => val(c.ticker, m)).filter(v => v != null)]
  let lo = Math.min(...vs)
  let hi = Math.max(...vs)
  if (hi === lo) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1 }
  return [lo, hi]
}
const sx = (m, v) => {
  const [lo, hi] = domain(m)
  const f = (v - lo) / (hi - lo)
  return 14 + 292 * (M(m).better === 'lower' ? 1 - f : f)
}
const diamond = (x, y) => `M${x} ${y - 6}L${x + 5} ${y}L${x} ${y + 6}L${x - 5} ${y}Z`
const jitter = (t) => {
  let h = 0
  for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) % 997
  return (h % 13) - 6
}

// ---------------------------------------------------------------- formats
const fmt = (m, v) => {
  if (v == null) return '—'
  if (M(m).unit === '%') return `${(v * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0)}%`
  const a = Math.abs(v)
  return `${v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2)}×`
}
const pctText = (t, m) => {
  const r = rank(t, m)
  return r ? `P${Math.round(r.pct)}` : ''
}
const sideText = (r) => !r ? '' : r.side === 'better' ? 'better than median' : r.side === 'worse' ? 'worse than median' : 'at median'
const flagText = (t, m) => flag(t, m) === 'nm' ? `N/M — ${M(m).nm || 'not meaningful'}` : 'no data'
const cellTitle = (t, m) => {
  if (val(t, m) == null) return flagText(t, m)
  const r = rank(t, m)
  const s = result.value?.benchmarks?.[basis.value]?.[level.value]?.[groupKey(t)]?.metrics?.[m]
  return `${M(m).name}: ${fmt(m, val(t, m))}\n${sideText(r)} (median ${fmt(m, s?.median)}), percentile ${Math.round(r?.pct ?? 0)}` +
    (r?.outlier ? '\noutlier — left out of the industry statistics' : '')
}

const periodText = computed(() => {
  const r = result.value
  if (!r) return ''
  return basis.value === 'ttm'
    ? `4 quarters published by ${r.cutoffs.quarter} (as_of − ${props.catalog.rules?.quarter_lag_days}d)`
    : `fiscal years ended by ${r.cutoffs.annual} (as_of − ${props.catalog.rules?.annual_lag_days}d)`
})
const basisWarning = computed(() => {
  const r = result.value
  if (!r || basis.value !== 'ttm' || r.coverage.ttm >= 0.8 * r.total) return ''
  return `Only ${r.coverage.ttm}/${r.total} companies have 4 quarters published before ${r.as_of_date} in yfinance (it keeps ~5 quarters). ` +
    'Use “Last fiscal year”, or a more recent as_of.'
})

// ------------------------------------------------------------- selection
const selectPassing = () => {
  const pass = new Set(passingAll.value)
  emit('set-excluded', props.candidates.filter(c => !pass.has(c.ticker)).map(c => c.ticker))
}
const selectAll = () => emit('set-excluded', [])
const clearAll = () => emit('set-excluded', props.candidates.map(c => c.ticker))

// lock payload
watch([state, lens, basis, level, threshold, disabled], () => {
  if (state.value !== 'done') return
  emit('config', {
    task_id: taskId.value, lens: lens.value, basis: basis.value, level: level.value,
    threshold: threshold.value, disabled: disabled.value
  })
}, { deep: true })
</script>

<style scoped>
.flab {
  --lens: #1D4ED8;
  --lens-soft: #EEF3FF;
  --better: #0F8A5F;
  --better-soft: #E3F5EC;
  --worse: #D13B3B;
  --worse-soft: #FBE9E9;
  max-width: 1440px;
  margin: 0 auto 40px;
  padding: 0 40px;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}
.flab.lens-growth { --lens: #FF5722; --lens-soft: #FFF1EC; }
.flab > * { position: relative; }
code { font-family: 'JetBrains Mono', monospace; font-size: 0.9em; background: #F0F0F0; padding: 1px 5px; border-radius: 3px; }

.flab-head {
  display: flex; justify-content: space-between; align-items: flex-end; gap: 20px;
  background: #000; color: #FFF; border-radius: 10px 10px 0 0; padding: 22px 24px 20px;
  background-image: radial-gradient(circle at 85% -40%, color-mix(in srgb, var(--lens) 55%, transparent), transparent 60%);
  transition: background-image 0.3s;
}
.flab-title { display: flex; gap: 14px; align-items: flex-start; }
.block-num {
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 800; width: 26px; height: 26px;
  display: grid; place-items: center; background: #FFF; color: #000; border-radius: 4px; flex-shrink: 0; margin-top: 3px;
}
.flab-title h2 { font-size: 1.7rem; font-weight: 600; letter-spacing: -0.8px; margin: 0 0 4px; }
.flab-title p { margin: 0; font-size: 13px; color: #BBB; max-width: 680px; line-height: 1.55; }
.pill { font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 5px 10px; border-radius: 20px; display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
.pill.ok { background: rgba(15,138,95,0.18); color: #6EE7B7; }
.pill.run { background: rgba(255,255,255,0.1); color: #FFF; }
.pill.idle { background: rgba(255,255,255,0.08); color: #999; }

/* ---- lens cards ---- */
.lens-row {
  display: grid; grid-template-columns: 1fr 1fr; gap: 0; background: #FFF;
  border: 1px solid #EAEAEA; border-top: none;
}
.lens-card {
  display: grid; grid-template-columns: 44px 1fr; grid-template-rows: auto auto; gap: 4px 14px; text-align: left;
  padding: 18px 22px; border: none; background: #FFF; cursor: pointer; position: relative; transition: background 0.2s;
  font-family: inherit; color: #000;
}
.lens-card + .lens-card { border-left: 1px solid #EAEAEA; }
.lens-card::after { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: transparent; transition: background 0.2s; }
.lens-card.lc-value { --c: #1D4ED8; --cs: #EEF3FF; }
.lens-card.lc-growth { --c: #FF5722; --cs: #FFF1EC; }
.lens-card.on { background: var(--cs); }
.lens-card.on::after { background: var(--c); }
.lens-card:not(.on) { opacity: 0.6; }
.lens-card:not(.on):hover { opacity: 1; }
.lens-icon { grid-row: span 2; width: 44px; height: 44px; border-radius: 10px; display: grid; place-items: center; background: #F2F2F2; transition: all 0.2s; }
.lens-card.on .lens-icon { background: var(--c); }
.lens-icon svg { width: 24px; height: 24px; fill: none; stroke: #555; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.lens-card.on .lens-icon svg { stroke: #FFF; }
.lens-text { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.lens-name { font-size: 1.25rem; font-weight: 700; letter-spacing: -0.3px; }
.lens-card.on .lens-name { color: var(--c); }
.lens-tag { font-size: 12.5px; color: #666; }
.lens-q { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #999; }

/* ---- intro ---- */
.intro, .loading, .controls, .metric-bar, .formulas, .bench, .rank, .sector-tabs {
  background: #FFF; border: 1px solid #EAEAEA; border-top: none;
}
.intro { padding: 22px 24px 24px; border-radius: 0 0 10px 10px; }
.intro-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
.intro-step { font-size: 12.5px; color: #555; line-height: 1.55; border-left: 2px solid #EEE; padding-left: 12px; }
.intro-step span { display: block; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--lens); font-weight: 800; margin-bottom: 2px; }
.intro-step b { display: block; color: #000; font-size: 13.5px; margin-bottom: 3px; }
.intro-cta { display: flex; align-items: center; gap: 16px; }
.load-btn {
  background: #000; color: #FFF; border: none; border-radius: 6px; padding: 14px 22px; cursor: pointer;
  font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 12.5px; letter-spacing: 0.5px;
  display: inline-flex; align-items: center; gap: 10px; transition: background 0.2s;
}
.load-btn:hover:not(:disabled) { background: var(--lens); }
.load-btn:disabled { background: #CCC; cursor: not-allowed; }
.intro-note { font-size: 12px; color: #999; }

/* ---- loading ---- */
.loading { padding: 26px 24px; border-radius: 0 0 10px 10px; }
.load-track { height: 6px; background: #F0F0F0; border-radius: 3px; overflow: hidden; }
.load-fill { height: 100%; background: linear-gradient(90deg, #000, var(--lens)); transition: width 0.4s; }
.load-text { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #666; margin: 12px 0 14px; }
.ticker-rain { display: flex; flex-wrap: wrap; gap: 6px; }
.ticker-rain span { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; padding: 3px 7px; border-radius: 4px; background: #F4F4F4; color: #BBB; transition: all 0.3s; }
.ticker-rain span.done { background: var(--lens-soft); color: var(--lens); }

/* ---- controls ---- */
.controls { display: flex; gap: 22px; align-items: flex-end; padding: 16px 24px; flex-wrap: wrap; }
.ctl { display: flex; flex-direction: column; gap: 6px; }
.ctl-label { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 700; color: #999; letter-spacing: 0.8px; text-transform: uppercase; }
.seg { display: inline-flex; background: #F2F2F2; border-radius: 7px; padding: 3px; gap: 2px; }
.seg button {
  border: none; background: transparent; padding: 7px 12px; border-radius: 5px; font-size: 12px; font-weight: 600;
  color: #666; cursor: pointer; font-family: inherit; display: inline-flex; gap: 6px; align-items: center; white-space: nowrap;
}
.seg button i { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
.seg button.on i { color: var(--lens); }
.seg.small button { padding: 5px 10px; font-size: 11px; text-transform: capitalize; }
.formula-btn {
  margin-left: auto; border: 1px solid #E0E0E0; background: #FFF; border-radius: 7px; padding: 9px 14px; cursor: pointer;
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 700;
}
.formula-btn.on, .formula-btn:hover { border-color: #000; background: #000; color: #FFF; }

.sector-tabs { display: flex; gap: 0; padding: 0 24px; overflow-x: auto; }
.sector-tabs button {
  border: none; background: none; padding: 12px 14px; font-size: 12.5px; font-weight: 600; color: #888; cursor: pointer;
  border-bottom: 2px solid transparent; font-family: inherit; white-space: nowrap;
}
.sector-tabs button i { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #BBB; margin-left: 4px; }
.sector-tabs button.on { color: #000; border-bottom-color: var(--lens); }

.metric-bar { display: flex; align-items: center; gap: 8px; padding: 14px 24px; flex-wrap: wrap; }
.mb-label { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; color: var(--lens); letter-spacing: 0.6px; text-transform: uppercase; margin-right: 6px; }
.mchip {
  border: 1px solid var(--lens); background: var(--lens-soft); color: #000; border-radius: 20px; padding: 5px 12px 5px 8px;
  font-size: 12px; font-weight: 600; cursor: pointer; display: inline-flex; gap: 6px; align-items: center; font-family: inherit;
  transition: all 0.15s;
}
.mchip-dir { width: 17px; height: 17px; border-radius: 50%; background: var(--lens); color: #FFF; display: grid; place-items: center; font-size: 10px; }
.mchip.off { border-color: #E0E0E0; background: #FFF; color: #AAA; text-decoration: line-through; }
.mchip.off .mchip-dir { background: #DDD; }
.mb-note { flex-basis: 100%; font-size: 12px; color: #888; margin-top: 4px; }

.formulas { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }
.f-col { padding: 16px 24px; }
.f-col + .f-col { border-left: 1px solid #F0F0F0; }
.f-head { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 8px; }
.fh-value { color: #1D4ED8; }
.fh-growth { color: #FF5722; }
.f-row { display: grid; grid-template-columns: 110px 1fr auto; gap: 2px 12px; padding: 7px 0; border-bottom: 1px dashed #EEE; font-size: 12px; }
.f-label { font-weight: 700; }
.f-formula { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: #333; }
.f-dir { font-size: 11px; color: #999; white-space: nowrap; }
.f-nm { grid-column: 2 / 4; font-size: 11px; color: #B07; }
.f-foot { grid-column: 1 / 3; display: flex; flex-direction: column; gap: 4px; padding: 12px 24px 16px; background: #FAFAFA; font-size: 11.5px; color: #666; border-top: 1px solid #F0F0F0; }
.fold-enter-active, .fold-leave-active { transition: opacity 0.2s; }
.fold-enter-from, .fold-leave-to { opacity: 0; }

/* ---- benchmark ---- */
.bench { padding: 20px 24px 24px; }
.bench-head { display: flex; justify-content: space-between; gap: 20px; align-items: flex-end; margin-bottom: 14px; flex-wrap: wrap; }
.eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; color: #999; letter-spacing: 1.4px; margin-bottom: 8px; }
.bench-group { display: flex; gap: 6px; flex-wrap: wrap; }
.gchip { border: 1px solid #E5E5E5; background: #FFF; border-radius: 6px; padding: 7px 11px; font-size: 12.5px; font-weight: 600; cursor: pointer; font-family: inherit; }
.gchip i { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; margin-left: 6px; }
.gchip.on { background: #000; color: #FFF; border-color: #000; }
.gchip.on i { color: var(--lens); filter: brightness(1.4); }
.bench-legend { display: flex; gap: 14px; font-size: 11px; color: #888; flex-wrap: wrap; }
.bench-legend span { display: inline-flex; align-items: center; gap: 5px; }
.bench-legend i { display: inline-block; }
.lg-box { width: 16px; height: 9px; background: #ECECEC; border: 1px solid #D5D5D5; border-radius: 2px; }
.lg-med { width: 2px; height: 12px; background: #000; }
.lg-mean { width: 8px; height: 8px; background: #FFF; border: 1.5px solid #000; transform: rotate(45deg); }
.lg-dot { width: 8px; height: 8px; border-radius: 50%; }
.lg-dot.peer { background: #BDBDBD; }
.lg-dot.mine { background: var(--lens); box-shadow: 0 0 0 2px #FFF, 0 0 0 3px var(--lens); }
.bench-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.bcard { border: 1px solid #EEE; border-radius: 8px; padding: 12px 14px 10px; background: #FFF; transition: box-shadow 0.2s; }
.bcard:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
.bcard-top { display: flex; align-items: baseline; gap: 8px; }
.bcard-label { font-weight: 700; font-size: 14px; }
.bcard-name { font-size: 11.5px; color: #999; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bcard-n { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; white-space: nowrap; }
.strip { width: 100%; height: 72px; display: block; overflow: visible; }
.strip .axis { stroke: #E5E5E5; stroke-width: 1; }
.strip .iqr { fill: #F0F0F0; stroke: #D8D8D8; }
.strip .median { stroke: #000; stroke-width: 2; }
.strip .mean { fill: #FFF; stroke: #000; stroke-width: 1.4; }
.strip .peer { fill: #BDBDBD; opacity: 0.85; }
.strip .peer.hidden { opacity: 0; }
.strip .mine { fill: var(--lens); stroke: #FFF; stroke-width: 2; cursor: pointer; transition: r 0.15s; }
.strip .mine.off { fill: #FFF; stroke: var(--lens); stroke-width: 1.5; }
.strip .mine.focus { stroke: #000; stroke-width: 2; }
.strip .dot-label { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; text-anchor: middle; fill: #000; }
.strip .end-label { font-family: 'JetBrains Mono', monospace; font-size: 8.5px; fill: #BBB; text-transform: uppercase; }
.bstats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; border-top: 1px dashed #EEE; padding-top: 8px; }
.bstats div { display: flex; flex-direction: column; }
.bstats span { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #AAA; text-transform: uppercase; letter-spacing: 0.5px; }
.bstats b { font-size: 13px; font-variant-numeric: tabular-nums; }
.bfocus { margin-top: 8px; font-size: 11.5px; color: #555; background: #FAFAFA; border-radius: 5px; padding: 5px 8px; }
.bfocus .better { color: var(--better); font-weight: 700; }
.bfocus .worse { color: var(--worse); font-weight: 700; }
.bfocus .nm { color: #999; font-style: italic; }
.bempty { font-size: 12px; color: #AAA; padding: 20px 0; text-align: center; }

/* ---- ranking ---- */
.rank { padding: 20px 24px 18px; border-radius: 0 0 10px 10px; }
.rank-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
.rank-sub { font-size: 13px; color: #555; }
.rank-sub b { color: var(--lens); font-size: 15px; }
.rank-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.act { border: 1px solid #E0E0E0; background: #FFF; border-radius: 6px; padding: 8px 12px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; }
.act:hover { border-color: #000; }
.act.primary { background: var(--lens); border-color: var(--lens); color: #FFF; font-family: 'JetBrains Mono', monospace; font-weight: 800; letter-spacing: 0.4px; }
.act.primary i { font-style: normal; background: rgba(255,255,255,0.25); border-radius: 10px; padding: 0 7px; margin-left: 4px; }
.act.primary:hover { filter: brightness(0.92); }

.rtable { overflow: auto; max-height: 640px; border: 1px solid #EEE; border-radius: 8px; }
.r-head, .r-row {
  display: grid; grid-template-columns: 40px minmax(200px, 1.4fr) repeat(var(--cols), minmax(92px, 1fr)) 64px 60px;
  align-items: center; min-width: calc(400px + var(--cols) * 92px);
}
.r-head { background: #FAFAFA; border-bottom: 1px solid #EEE; position: sticky; top: 0; z-index: 2; }
.r-head span { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; color: #999; text-transform: uppercase; letter-spacing: 0.4px; padding: 10px 8px; white-space: nowrap; }
.r-head em { font-style: normal; color: var(--lens); }
.sortable { cursor: pointer; user-select: none; }
.sortable.on { color: #000 !important; }
.r, .r-head .r { text-align: right; }
.c { text-align: center; justify-content: center; display: flex; }
.r-row { border-bottom: 1px solid #F3F3F3; cursor: pointer; transition: background 0.12s; }
.r-row:last-child { border-bottom: none; }
.r-row:hover { background: #FAFAFA; }
.r-row.focus { background: var(--lens-soft); }
.r-row:not(.sel) { opacity: 0.55; }
.r-row:not(.sel):hover { opacity: 0.85; }
.r-check { display: grid; place-items: center; height: 100%; }
.check {
  width: 18px; height: 18px; border-radius: 4px; border: 1.5px solid #CCC; display: grid; place-items: center; font-size: 11px;
  color: transparent; transition: all 0.15s;
}
.check.on { background: var(--lens); border-color: var(--lens); color: #FFF; }
.r-company { display: flex; flex-direction: column; padding: 9px 8px; min-width: 0; }
.r-company b { font-family: 'JetBrains Mono', monospace; font-size: 13px; }
.r-row.pass .r-company b::after { content: ' ●'; color: var(--better); font-size: 9px; vertical-align: middle; }
.r-name { font-size: 12px; color: #444; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.r-ind { font-size: 10.5px; color: #AAA; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mcell { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; padding: 8px; }
.mval { font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums; }
.mval sup { font-family: 'JetBrains Mono', monospace; font-size: 7.5px; font-weight: 800; margin-left: 3px; padding: 1px 3px; border-radius: 2px; background: #000; color: #FFF; vertical-align: 4px; }
.mbar { width: 100%; max-width: 80px; height: 4px; background: #F0F0F0; border-radius: 2px; overflow: hidden; display: block; }
.mbar i { display: block; height: 100%; border-radius: 2px; background: #BBB; }
.mcell.better .mval { color: var(--better); }
.mcell.better .mbar i { background: var(--better); }
.mcell.worse .mval { color: var(--worse); }
.mcell.worse .mbar i { background: var(--worse); }
.mcell.equal .mbar i { background: #888; }
.mna { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #BBB; font-style: italic; }
.beats { font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 800; padding: 3px 7px; border-radius: 5px; background: var(--worse-soft); color: var(--worse); }
.beats.ok { background: var(--better-soft); color: var(--better); }
.ring { width: 36px; height: 36px; }
.ring-bg { fill: none; stroke: #EFEFEF; stroke-width: 3.5; }
.ring-fg { fill: none; stroke: var(--lens); stroke-width: 3.5; stroke-linecap: round; transform: rotate(-90deg); transform-origin: 18px 18px; }
.ring text { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; }
.r-empty { padding: 24px; text-align: center; color: #AAA; font-size: 12px; }
.rank-foot { font-size: 11.5px; color: #999; line-height: 1.6; margin: 12px 0 0; }
.better-t { color: var(--better); font-weight: 700; }
.worse-t { color: var(--worse); font-weight: 700; }

.msg { margin: 0; padding: 9px 24px; font-size: 12px; border: 1px solid #EAEAEA; border-top: none; }
.msg.warn { background: #FFF8E6; color: #8A5A00; }
.msg.error { background: #FDECEC; color: #B42318; border: none; padding: 9px 0; margin-top: 10px; }
.bench .msg.warn { border: none; border-radius: 6px; margin-bottom: 12px; padding: 8px 12px; }

.spin { width: 11px; height: 11px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #FFF; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 900px) {
  .flab { padding: 0 16px; }
  .lens-row, .formulas { grid-template-columns: 1fr; }
  .lens-card + .lens-card { border-left: none; border-top: 1px solid #EAEAEA; }
  .intro-grid { grid-template-columns: 1fr 1fr; }
  .f-col + .f-col { border-left: none; }
}
</style>
