<template>
  <section class="flab" :style="{ '--cat': catColor(category), '--cat-soft': catSoft(category) }">
    <!-- ============ header ============ -->
    <div class="flab-head">
      <div class="flab-title">
        <span class="block-num">F</span>
        <div>
          <h2>Fundamental Lab</h2>
          <p>Pick a stock category and its ratios, see every company on a number line against its sector's
            min · median · average · max, and narrow the list down to the companies worth a closer look.</p>
        </div>
      </div>
      <div class="flab-status">
        <span v-if="state === 'done'" class="pill ok">● {{ result.total - Object.keys(result.failed || {}).length }}/{{ result.total }} companies · as_of {{ result.as_of_date }}</span>
        <span v-else-if="state === 'running'" class="pill run"><span class="spin"></span> {{ progress.current === 'price history' ? 'price history' : `${progress.processed}/${progress.total || '…'}` }}</span>
        <span v-else-if="!screenReady" class="pill idle">waiting for the screen</span>
      </div>
    </div>

    <!-- ============ category picker (always visible) ============ -->
    <div class="cat-row">
      <button v-for="key in catalog.category_order || []" :key="key" class="cat-card" :class="{ on: category === key }"
              :style="{ '--c': catColor(key), '--cs': catSoft(key) }" @click="category = key">
        <span class="cat-name">{{ catalog.categories[key].label }}</span>
        <span class="cat-tag">{{ catalog.categories[key].tagline }}</span>
        <span class="cat-count">{{ catalog.categories[key].metrics.length }} ratios</span>
      </button>
    </div>

    <!-- ============ ratio checklist ============ -->
    <div class="ratio-box">
      <div class="ratio-head">
        <div>
          <span class="rb-q">{{ catalog.categories[category]?.question }}</span>
          <span class="rb-src">{{ catalog.categories[category]?.source }}</span>
        </div>
        <div class="rb-actions">
          <label class="all-toggle" :class="{ on: allOn }">
            <input type="checkbox" :checked="allOn" :indeterminate.prop="!allOn && selectedList.length > 0" @change="toggleAll" />
            All ratios
          </label>
          <button class="link" @click="useRecommended" :title="`Recommended for ${sectorsForDefaults.join(', ') || 'the chosen sectors'}`">Recommended</button>
          <button class="formula-btn" :class="{ on: showFormulas }" @click="showFormulas = !showFormulas">ƒ Formulas</button>
        </div>
      </div>
      <div class="ratio-chips">
        <button v-for="m in categoryList" :key="m" class="rchip" :class="{ on: isSel(m) }" @click="toggleMetric(m)"
                :title="`${M(m).name} = ${M(m).formula}${M(m).rule ? '\nRule of thumb — ' + M(m).rule : ''}`">
          <span class="rchip-box">{{ isSel(m) ? '✓' : '' }}</span>
          {{ M(m).label }}
          <span class="rchip-dir">{{ M(m).better === 'lower' ? '↓' : '↑' }}</span>
          <span v-if="naSectors(m).length" class="rchip-na">n/a {{ naSectors(m).map(shortSector).join(', ') }}</span>
        </button>
      </div>
      <transition name="fold">
        <div v-if="showFormulas" class="formulas">
          <div v-for="m in categoryList" :key="m" class="f-row">
            <span class="f-label">{{ M(m).label }}</span>
            <span class="f-formula">{{ M(m).formula }}</span>
            <span class="f-dir">{{ M(m).better === 'lower' ? '↓ lower' : '↑ higher' }} is better</span>
            <span class="f-rule" v-if="M(m).rule">Rule of thumb — {{ M(m).rule }}</span>
            <span class="f-nm" v-if="M(m).nm">N/M when {{ M(m).nm }}</span>
          </div>
          <div class="f-foot">
            <span><b>Quarter views:</b> flows = sum of 4 quarters (TTM) ending at that quarter, balance sheet at its end; {{ catalog.growth_period_note?.quarter }}.</span>
            <span><b>Year views:</b> one fiscal year; {{ catalog.growth_period_note?.year }}.</span>
            <span><b>Price:</b> the latest quarter / year use the close on as_of (today's valuation); the previous ones the close at their period end (the valuation then). Market cap = price × that period's share count.</span>
            <span><b>Statistics:</b> N/M values are left out; {{ catalog.rules?.outlier }}. Rules of thumb are guidance only — the filter compares with the group median.</span>
          </div>
        </div>
      </transition>
    </div>

    <!-- ============ not loaded yet ============ -->
    <div v-if="state === 'idle' || state === 'failed'" class="intro">
      <div class="intro-grid">
        <div class="intro-step"><span>01</span><b>Statements</b>Last 2 quarters (as 4-quarter TTM) and last 2 fiscal years from yfinance, only periods published before <code>as_of</code>.</div>
        <div class="intro-step"><span>02</span><b>Ratios</b>Six categories — value, growth, dividend, quality, GARP, defensive — pick all ratios or some.</div>
        <div class="intro-step"><span>03</span><b>Number line</b>Min · median · average · max of every ratio for the whole sector (all tiers), with each ticker placed on it.</div>
        <div class="intro-step"><span>04</span><b>Shortlist</b>Click a ticker or drag over a range to remove companies, down to the 10–15 worth a closer look.</div>
      </div>
      <div class="intro-cta">
        <button class="load-btn" :disabled="!screenReady || starting" @click="load">
          <span v-if="starting" class="spin light"></span>
          LOAD FUNDAMENTALS · {{ peerCount }} COMPANIES →
        </button>
        <span class="intro-note" v-if="!screenReady">Finish the universe screen first.</span>
        <span class="intro-note" v-else>≈ {{ Math.max(1, Math.round(peerCount / 4 / 60)) }} min the first time today, faster after (statements cached per day).</span>
      </div>
      <div v-if="error" class="msg error">{{ error }}</div>
    </div>

    <!-- ============ loading ============ -->
    <div v-else-if="state === 'running'" class="loading">
      <div class="load-track"><div class="load-fill" :style="{ width: progressPct + '%' }"></div></div>
      <div class="load-text">{{ progress.current === 'price history' ? 'Downloading 3 years of prices' : 'Reading financial statements' }} · <b>{{ progress.current || '…' }}</b></div>
      <div class="ticker-rain">
        <span v-for="t in rainTickers" :key="t" :class="{ done: rainDone.has(t) }">{{ t }}</span>
      </div>
    </div>

    <!-- ============ loaded ============ -->
    <template v-else-if="state === 'done'">
      <div class="controls">
        <div class="ctl">
          <span class="ctl-label">Period</span>
          <div class="seg">
            <button v-for="p in catalog.period_order" :key="p" :class="{ on: period === p, empty: !result.coverage[p] }"
                    @click="period = p" :title="catalog.periods[p].desc">
              {{ catalog.periods[p].label }}
              <i>{{ periodEnd(p) }}</i>
            </button>
          </div>
        </div>
        <div class="ctl">
          <span class="ctl-label">Benchmark against</span>
          <div class="seg">
            <button :class="{ on: level === 'sector' }" @click="level = 'sector'">Sector</button>
            <button :class="{ on: level === 'sub_industry' }" @click="level = 'sub_industry'">Industry</button>
          </div>
        </div>
        <div class="ctl">
          <span class="ctl-label">View</span>
          <div class="seg">
            <button :class="{ on: view === 'line' }" @click="view = 'line'">▭ Number line</button>
            <button :class="{ on: view === 'dist' }" @click="view = 'dist'">⋯ Distribution</button>
          </div>
        </div>
        <div class="ctl" v-if="view === 'line'">
          <span class="ctl-label">Line width · {{ Math.round(zoom * 100) }}%</span>
          <div class="zoom">
            <button @click="zoom = Math.max(1, +(zoom - 0.25).toFixed(2))">−</button>
            <input type="range" min="1" max="4" step="0.25" v-model.number="zoom" />
            <button @click="zoom = Math.min(4, +(zoom + 0.25).toFixed(2))">+</button>
            <button class="fit" @click="zoom = autoZoom">fit</button>
          </div>
        </div>
        <label class="ctl show-rm">
          <input type="checkbox" v-model="showRemoved" /> show removed
        </label>
      </div>
      <div v-if="periodWarning" class="msg warn">{{ periodWarning }}</div>

      <!-- groups shown (industry mode: pick industries) -->
      <div class="groups">
        <span class="g-label">{{ level === 'sector' ? 'Sectors' : 'Industries' }}</span>
        <template v-if="level === 'sector'">
          <span v-for="g in allGroups" :key="g.name" class="gchip on static">{{ g.name }} <i>{{ g.peers }} cos · {{ g.candidates }} yours</i></span>
        </template>
        <template v-else>
          <button v-for="g in allGroups" :key="g.name" class="gchip" :class="{ on: shownGroups.includes(g.name) }" @click="toggleGroup(g.name)">
            {{ g.name }} <i>{{ g.peers }} cos · {{ g.candidates }} yours</i>
          </button>
        </template>
      </div>

      <!-- shortlist bar -->
      <div class="shortlist">
        <div class="sl-count">
          <b>{{ keptCount }}</b> kept <span>of {{ candidates.length }}</span>
          <span class="sl-target" :class="{ ok: keptCount >= 10 && keptCount <= 15 }">target 10–15</span>
        </div>
        <div class="sl-legend">
          <span><i style="background:#E53935"></i>min</span>
          <span><i style="background:#1E63E9"></i>median</span>
          <span><i class="dash" style="border-color:#16A34A"></i>average</span>
          <span><i style="background:#111"></i>max</span>
          <span class="tip">click a ticker to remove / restore · drag across a line to pick a range</span>
        </div>
        <div class="sl-actions">
          <span class="rule">passes = beats the median on
            <select v-model.number="threshold">
              <option v-for="t in catalog.thresholds" :key="t" :value="t">{{ t === 1 ? 'all' : `≥ ${Math.round(t * 100)}%` }}</option>
            </select>
            of the ratios
          </span>
          <button class="act primary" @click="selectPassing">✓ Keep passing <i>{{ passingAll.length }}</i></button>
          <button class="act" @click="selectAll">Keep all</button>
          <button class="act" @click="clearAll">Remove all</button>
        </div>
      </div>

      <!-- ============ NUMBER LINE VIEW ============ -->
      <div v-if="view === 'line'" class="lines" ref="linesRef">
        <div v-if="!selectedList.length" class="empty">Pick at least one ratio above.</div>
        <div v-for="m in selectedList" :key="m" class="lcard">
          <div class="lcard-head">
            <span class="lc-label">{{ M(m).label }}</span>
            <span class="lc-name">{{ M(m).name }} · {{ M(m).formula }}</span>
            <span class="lc-dir">{{ M(m).better === 'lower' ? '← lower is better' : 'higher is better →' }}</span>
            <button class="lc-act" @click="removeWorse(m)" :title="'Remove every kept company that is worse than its group median on ' + M(m).label">
              remove worse than median
            </button>
          </div>
          <div v-for="g in shownGroups" :key="g" class="lane">
            <div class="lane-head">
              <b>{{ g }}</b>
              <template v-if="gstat(g, m)?.n">
                <span>n={{ gstat(g, m).n }}</span>
                <span v-if="gstat(g, m).nm">{{ gstat(g, m).nm }} N/M</span>
                <span v-if="gstat(g, m).outliers.length">{{ gstat(g, m).outliers.length }} outlier{{ gstat(g, m).outliers.length > 1 ? 's' : '' }}</span>
                <span v-if="laneNm(g, m).length" class="nm-list" :title="laneNm(g, m).join(', ')">N/M: {{ laneNm(g, m).slice(0, 8).join(', ') }}{{ laneNm(g, m).length > 8 ? '…' : '' }}</span>
              </template>
            </div>
            <div v-if="!applies(g, m)" class="lane-empty">{{ M(m).label }} does not apply to {{ g }}.</div>
            <div v-else-if="!gstat(g, m)?.n" class="lane-empty">No company in {{ g }} has a meaningful {{ M(m).label }} for this period.</div>
            <div v-else class="lane-scroll">
              <NumberLine :stats="gstat(g, m)" :items="laneItems(g, m)" :width="lineWidth" :fmt="(v) => fmt(m, v)"
                          :focus="hoverTicker" @toggle="(t) => $emit('toggle', t)" @hover="(t) => (hoverTicker = t)"
                          @brush="onBrush" />
            </div>
          </div>
        </div>
      </div>

      <!-- ============ DISTRIBUTION VIEW ============ -->
      <div v-else class="bench">
        <div v-for="g in shownGroups" :key="g" class="dist-group">
          <div class="dist-title">{{ g }}</div>
          <div class="bench-grid">
            <div v-for="m in selectedList" :key="m" class="bcard">
              <div class="bcard-top">
                <span class="bcard-label">{{ M(m).label }}</span>
                <span class="bcard-name">{{ M(m).name }}</span>
                <span class="bcard-n">n={{ gstat(g, m)?.n || 0 }}</span>
              </div>
              <template v-if="applies(g, m) && gstat(g, m)?.n">
                <svg class="strip" viewBox="0 0 320 64" preserveAspectRatio="none">
                  <line x1="10" x2="310" y1="34" y2="34" class="axis" />
                  <rect :x="Math.min(dx(g, m, gstat(g, m).q1), dx(g, m, gstat(g, m).q3))" y="22"
                        :width="Math.max(2, Math.abs(dx(g, m, gstat(g, m).q3) - dx(g, m, gstat(g, m).q1)))" height="24" rx="3" class="iqr" />
                  <line :x1="dx(g, m, gstat(g, m).min)" :x2="dx(g, m, gstat(g, m).min)" y1="18" y2="50" stroke="#E53935" stroke-width="1.6" />
                  <line :x1="dx(g, m, gstat(g, m).max)" :x2="dx(g, m, gstat(g, m).max)" y1="18" y2="50" stroke="#111" stroke-width="1.6" />
                  <line :x1="dx(g, m, gstat(g, m).median)" :x2="dx(g, m, gstat(g, m).median)" y1="14" y2="54" stroke="#1E63E9" stroke-width="2" />
                  <line :x1="dx(g, m, gstat(g, m).mean)" :x2="dx(g, m, gstat(g, m).mean)" y1="14" y2="54" stroke="#16A34A" stroke-width="2" stroke-dasharray="4 3" />
                  <circle v-for="it in laneItems(g, m)" :key="it.ticker" :cx="dx(g, m, it.value)" cy="34" r="4.5"
                          class="mine" :class="{ off: it.removed }" @click="$emit('toggle', it.ticker)">
                    <title>{{ it.ticker }} · {{ fmt(m, it.value) }}</title>
                  </circle>
                </svg>
                <div class="bstats">
                  <div><span style="color:#E53935">min</span><b>{{ fmt(m, gstat(g, m).min) }}</b></div>
                  <div><span style="color:#1E63E9">median</span><b>{{ fmt(m, gstat(g, m).median) }}</b></div>
                  <div><span style="color:#16A34A">average</span><b>{{ fmt(m, gstat(g, m).mean) }}</b></div>
                  <div><span>max</span><b>{{ fmt(m, gstat(g, m).max) }}</b></div>
                </div>
              </template>
              <div v-else class="bempty">{{ applies(g, m) ? 'No meaningful values.' : 'Does not apply to this sector.' }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ ranking ============ -->
      <div class="rank">
        <div class="rank-head">
          <div>
            <div class="eyebrow">RANKING · {{ catalog.categories[category].label.toUpperCase() }} · {{ catalog.periods[period].label.toUpperCase() }}</div>
            <div class="rank-sub">
              <b>{{ passingAll.length }}</b> of {{ candidates.length }} companies beat their {{ level === 'sector' ? 'sector' : 'industry' }} median on
              {{ threshold === 1 ? 'every' : `at least ${Math.round(threshold * 100)}% of the` }} chosen ratios.
            </div>
          </div>
          <div class="seg small">
            <button v-for="v in ['all', 'passing', 'kept']" :key="v" :class="{ on: show === v }" @click="show = v">{{ v }}</button>
          </div>
        </div>
        <div class="rtable" :style="{ '--cols': selectedList.length }">
          <div class="r-head">
            <span></span>
            <span class="sortable" :class="{ on: sortKey === 'ticker' }" @click="sortBy('ticker')">Company</span>
            <span v-for="m in selectedList" :key="m" class="sortable r" :class="{ on: sortKey === m }" @click="sortBy(m)" :title="M(m).formula">
              {{ M(m).label }} <em>{{ M(m).better === 'lower' ? '↓' : '↑' }}</em>
            </span>
            <span class="sortable c" :class="{ on: sortKey === 'beats' }" @click="sortBy('beats')">Beats</span>
            <span class="sortable c" :class="{ on: sortKey === 'score' }" @click="sortBy('score')">Score</span>
          </div>
          <div v-for="row in tableRows" :key="row.ticker" class="r-row"
               :class="{ sel: row.selected, focus: hoverTicker === row.ticker, pass: row.ev.passes }"
               @mouseenter="hoverTicker = row.ticker" @mouseleave="hoverTicker = ''" @click="$emit('toggle', row.ticker)">
            <span class="r-check"><span class="check" :class="{ on: row.selected }">✓</span></span>
            <span class="r-company">
              <b>{{ row.ticker }}</b>
              <span class="r-name">{{ row.company_name }}</span>
              <span class="r-ind">{{ row.group }}<template v-if="row.tier"> · {{ row.tier }}</template></span>
            </span>
            <span v-for="m in selectedList" :key="m" class="mcell" :class="row.ev.ranks[m]?.side || 'none'" :title="cellTitle(row, m)">
              <template v-if="row.ev.ranks[m]">
                <span class="mval">{{ fmt(m, val(row.ticker, m)) }}<sup v-if="row.ev.ranks[m].extreme" :class="row.ev.ranks[m].extreme">{{ row.ev.ranks[m].extreme.toUpperCase() }}</sup></span>
                <span class="mbar"><i :style="{ width: row.ev.ranks[m].pct + '%' }"></i></span>
              </template>
              <span v-else class="mna">{{ !row.ev.metrics.includes(m) ? 'n/a' : flag(row.ticker, m) === 'nm' ? 'N/M' : '—' }}</span>
            </span>
            <span class="r-beats c"><span class="beats" :class="{ ok: row.ev.passes }">{{ row.ev.beat_count }}/{{ row.ev.n }}</span></span>
            <span class="r-score c">
              <svg viewBox="0 0 36 36" class="ring">
                <circle cx="18" cy="18" r="15" class="ring-bg" />
                <circle v-if="row.ev.score != null" cx="18" cy="18" r="15" class="ring-fg" :stroke-dasharray="`${row.ev.score * 0.9425} 94.25`" />
                <text x="18" y="22" text-anchor="middle">{{ row.ev.score != null ? Math.round(row.ev.score) : '–' }}</text>
              </svg>
            </span>
          </div>
          <div v-if="!tableRows.length" class="r-empty">No companies in this view.</div>
        </div>
        <p class="rank-foot">
          Click a row to keep / remove. Cells: value + percentile bar within the {{ level === 'sector' ? 'sector' : 'industry' }}
          (full = best, direction-adjusted). <span class="better-t">Green</span> = better than the median, <span class="worse-t">red</span> = worse;
          MIN / MAX = the group's lowest / highest value. Score = average percentile (N/M counts as 0).
          Period: {{ catalog.periods[period].desc }}.
        </p>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { startFundamentals, getFundamentals } from '../../api/pipeline'
import NumberLine from './NumberLine.vue'

const props = defineProps({
  catalog: { type: Object, default: () => ({ categories: {}, category_order: [], metrics: {}, applicable: {}, recommended: {}, periods: {}, period_order: [], thresholds: [0.5] }) },
  screenTaskId: { type: String, default: null },
  screenReady: { type: Boolean, default: false },
  peerCount: { type: Number, default: 0 },
  // rows the user can keep: {ticker, company_name, gics_sector, gics_sub_industry, tier, state: 'in' | 'excluded'}
  candidates: { type: Array, default: () => [] }
})
const emit = defineEmits(['toggle', 'set-excluded', 'config', 'evals'])

const CAT_COLORS = {
  value: ['#6D28D9', '#F3EEFF'], growth: ['#FF5722', '#FFF1EC'], dividend: ['#0E7490', '#E6F6F9'],
  quality: ['#B45309', '#FDF4E7'], garp: ['#BE185D', '#FDEDF4'], defensive: ['#475569', '#F1F4F8']
}
const catColor = (k) => (CAT_COLORS[k] || CAT_COLORS.value)[0]
const catSoft = (k) => (CAT_COLORS[k] || CAT_COLORS.value)[1]
const M = (m) => props.catalog.metrics?.[m] || { label: m, name: m, formula: '', better: 'higher', unit: 'x' }
const SHORT = { 'Information Technology': 'IT', 'Communication Services': 'Comm', 'Consumer Discretionary': 'Disc', 'Consumer Staples': 'Staples', 'Health Care': 'HC', 'Real Estate': 'RE' }
const shortSector = (s) => SHORT[s] || s

// ------------------------------------------------------------- state
const state = ref('idle')          // idle | running | done | failed
const starting = ref(false)
const error = ref('')
const taskId = ref(null)
const result = ref(null)
const progress = ref({ processed: 0, total: 0, current: '' })
const rainDone = ref(new Set())
let timer = null

const category = ref('value')
const period = ref('q0')
const level = ref('sector')
const threshold = ref(0.5)
const view = ref('line')
const zoom = ref(1)
const showRemoved = ref(true)
const showFormulas = ref(false)
const selected = ref({})            // category -> [metric keys]
const pickedGroups = ref([])        // industries shown in industry mode
const hoverTicker = ref('')
const sortKey = ref('score')
const sortDir = ref(-1)
const show = ref('all')

// ------------------------------------------------------------- ratio selection
const sectorsForDefaults = computed(() => [...new Set(props.candidates.map(c => c.gics_sector))].sort())
const categoryList = computed(() => props.catalog.categories?.[category.value]?.metrics || [])
const applicableIn = (s, m) => (props.catalog.applicable?.[s]?.[category.value] || []).includes(m)
const naSectors = (m) => sectorsForDefaults.value.filter(s => props.catalog.applicable?.[s] && !applicableIn(s, m))
// a ratio that applies to none of the chosen sectors cannot be picked
const usable = (m) => !sectorsForDefaults.value.length || naSectors(m).length < sectorsForDefaults.value.length
const recommendedList = () => {
  const rec = new Set()
  sectorsForDefaults.value.forEach(s => (props.catalog.recommended?.[s]?.[category.value] || []).forEach(m => rec.add(m)))
  const list = categoryList.value.filter(m => rec.has(m))
  return list.length ? list : categoryList.value.filter(usable)
}
const selectedList = computed(() => {
  const chosen = selected.value[category.value]
  return chosen ? categoryList.value.filter(m => chosen.includes(m)) : recommendedList()
})
const isSel = (m) => selectedList.value.includes(m)
const allOn = computed(() => categoryList.value.filter(usable).every(isSel))
const setSel = (list) => { selected.value = { ...selected.value, [category.value]: list } }
const toggleMetric = (m) => {
  const cur = selectedList.value
  setSel(cur.includes(m) ? cur.filter(x => x !== m) : [...cur, m])
}
const toggleAll = () => setSel(allOn.value ? [] : categoryList.value.filter(usable))
const useRecommended = () => setSel(recommendedList())
watch(sectorsForDefaults, () => { selected.value = {} })   // new sectors -> fresh recommended sets

// ------------------------------------------------------------- loading
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
onUnmounted(stop)

const progressPct = computed(() => progress.value.total ? Math.round(progress.value.processed * 100 / progress.value.total) : 2)
const rainTickers = computed(() => props.candidates.map(c => c.ticker).slice(0, 80))

const poll = async () => {
  try {
    const { data: task } = await getFundamentals(taskId.value)
    const d = task.progress_detail || {}
    progress.value = { processed: d.processed || 0, total: d.total || 0, current: d.current || '' }
    if (d.current && d.current !== 'price history') rainDone.value = new Set([...rainDone.value, d.current])
    if (task.status === 'completed') {
      stop()
      result.value = task.result
      const r = task.result
      period.value = props.catalog.period_order.find(p => r.coverage[p] >= 0.8 * r.total) || 'fy0'
      state.value = 'done'
      await nextTick()
      zoom.value = autoZoom.value
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

// ------------------------------------------------------------- data access
const T = (t) => result.value?.tickers?.[t]
const val = (t, m) => T(t)?.[period.value]?.values?.[m] ?? null
const flag = (t, m) => T(t)?.[period.value]?.flags?.[m] || 'na'
const groupOf = (t) => level.value === 'sector' ? T(t)?.sector : T(t)?.sub_industry
const bench = computed(() => result.value?.benchmarks?.[period.value]?.[level.value] || {})
const gstat = (g, m) => bench.value[g]?.metrics?.[m]
const sectorOfGroup = (g) => level.value === 'sector' ? g : (props.candidates.find(c => T(c.ticker)?.sub_industry === g)?.gics_sector)
const applies = (g, m) => applicableIn(sectorOfGroup(g), m)

const known = computed(() => props.candidates.filter(c => T(c.ticker)))
const allGroups = computed(() => {
  const g = {}
  known.value.forEach(c => { const k = groupOf(c.ticker); g[k] = (g[k] || 0) + 1 })
  return Object.entries(g).map(([name, n]) => ({ name, candidates: n, peers: bench.value[name]?.peers || 0 }))
    .sort((a, b) => b.candidates - a.candidates || a.name.localeCompare(b.name))
})
const shownGroups = computed(() => level.value === 'sector'
  ? allGroups.value.map(g => g.name)
  : allGroups.value.map(g => g.name).filter(n => pickedGroups.value.includes(n)))
const toggleGroup = (name) => {
  pickedGroups.value = pickedGroups.value.includes(name)
    ? pickedGroups.value.filter(n => n !== name) : [...pickedGroups.value, name]
}
watch([level, allGroups], () => {
  if (level.value === 'sub_industry' && !shownGroups.value.length) pickedGroups.value = allGroups.value.slice(0, 2).map(g => g.name)
})

const isKept = (t) => props.candidates.find(c => c.ticker === t)?.state === 'in'
const keptCount = computed(() => props.candidates.filter(c => c.state === 'in').length)
const laneItems = (g, m) => known.value
  .filter(c => groupOf(c.ticker) === g && val(c.ticker, m) != null && (showRemoved.value || c.state === 'in'))
  .map(c => ({ ticker: c.ticker, value: val(c.ticker, m), removed: c.state !== 'in' }))
const laneNm = (g, m) => known.value.filter(c => groupOf(c.ticker) === g && flag(c.ticker, m) === 'nm').map(c => c.ticker)

// ------------------------------------------------------------- ranks (mirror of backend rank_in_group)
const rankIn = (value, ticker, stats, better) => {
  if (value == null || !stats?.n) return null
  const others = stats.values.filter(([t]) => t !== ticker).map(([, v]) => v)
  const pctLow = others.length
    ? (others.filter(v => v < value).length + 0.5 * others.filter(v => v === value).length) / others.length * 100
    : 50
  const pct = better === 'higher' ? pctLow : 100 - pctLow
  const side = value === stats.median ? 'equal' : ((value > stats.median) === (better === 'higher') ? 'better' : 'worse')
  const extreme = value >= stats.max ? 'max' : value <= stats.min ? 'min' : null
  return { pct, side, extreme }
}
// same rule as backend fundamentals.evaluate
const evaluate = (t) => {
  const g = groupOf(t)
  const sector = T(t)?.sector
  const ms = selectedList.value.filter(m => applicableIn(sector, m))
  const ranks = {}
  ms.forEach(m => { ranks[m] = rankIn(val(t, m), t, gstat(g, m), M(m).better) })
  const beats = ms.filter(m => ranks[m]?.side === 'better')
  const pcts = ms.filter(m => ranks[m] || flag(t, m) === 'nm').map(m => ranks[m]?.pct ?? 0)
  return {
    metrics: ms, ranks, n: ms.length, beat_count: beats.length,
    score: pcts.length && pcts.length * 2 >= ms.length ? pcts.reduce((a, b) => a + b, 0) / pcts.length : null,
    passes: ms.length > 0 && beats.length >= Math.ceil(threshold.value * ms.length - 1e-9)
  }
}
const evals = computed(() => {
  const out = {}
  known.value.forEach(c => { out[c.ticker] = evaluate(c.ticker) })
  return out
})
const passingAll = computed(() => known.value.filter(c => evals.value[c.ticker]?.passes).map(c => c.ticker))
// the final-list table shows each company's result under the current settings
watch(evals, (ev) => {
  const out = {}
  Object.entries(ev).forEach(([t, e]) => { out[t] = { passes: e.passes, beat_count: e.beat_count, n: e.n, score: e.score } })
  emit('evals', out)
})

// ------------------------------------------------------------- shortlist actions
const excludedNow = () => props.candidates.filter(c => c.state !== 'in').map(c => c.ticker)
const setExcluded = (list) => emit('set-excluded', [...new Set(list)])
const selectPassing = () => {
  const pass = new Set(passingAll.value)
  setExcluded(props.candidates.filter(c => !pass.has(c.ticker)).map(c => c.ticker))
}
const selectAll = () => setExcluded([])
const clearAll = () => setExcluded(props.candidates.map(c => c.ticker))
const removeWorse = (m) => {
  const worse = known.value.filter(c => evals.value[c.ticker]?.ranks[m]?.side === 'worse' || (evals.value[c.ticker]?.metrics.includes(m) && flag(c.ticker, m) === 'nm'))
  setExcluded([...excludedNow(), ...worse.map(c => c.ticker)])
}
const onBrush = ({ action, tickers }) => {
  const hit = new Set(tickers)
  if (action === 'remove') setExcluded([...excludedNow(), ...tickers])
  else setExcluded(props.candidates.filter(c => !hit.has(c.ticker)).map(c => c.ticker))
}

// ------------------------------------------------------------- table
const sortBy = (k) => {
  if (sortKey.value === k) sortDir.value *= -1
  else { sortKey.value = k; sortDir.value = k === 'ticker' ? 1 : -1 }
}
const tableRows = computed(() => {
  let rows = known.value.filter(c => shownGroups.value.includes(groupOf(c.ticker))).map(c => ({
    ...c, selected: c.state === 'in', group: groupOf(c.ticker), ev: evals.value[c.ticker]
  }))
  if (show.value === 'passing') rows = rows.filter(r => r.ev.passes)
  if (show.value === 'kept') rows = rows.filter(r => r.selected)
  const k = sortKey.value
  const key = (r) => k === 'ticker' ? r.ticker
    : k === 'score' ? (r.ev.score ?? -1)
      : k === 'beats' ? r.ev.beat_count + (r.ev.score ?? 0) / 1000
        : (r.ev.ranks[k]?.pct ?? -1)
  return rows.sort((a, b) => {
    const x = key(a)
    const y = key(b)
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir.value
  })
})

// ------------------------------------------------------------- number line sizing
const linesRef = ref(null)
const baseWidth = ref(1100)
let ro = null
onMounted(() => {
  ro = new ResizeObserver(() => { if (linesRef.value) baseWidth.value = Math.max(600, linesRef.value.clientWidth - 34) })
  watch(linesRef, (el) => { if (el) ro.observe(el) }, { immediate: true })
})
onUnmounted(() => ro?.disconnect())
const lineWidth = computed(() => Math.round(baseWidth.value * zoom.value))
// widest lane: enough room for its labels in about 3 rows
const autoZoom = computed(() => {
  let need = 0
  shownGroups.value.forEach(g => {
    const n = known.value.filter(c => groupOf(c.ticker) === g).length
    need = Math.max(need, n * 17 * 6.4 / 3)
  })
  return Math.min(4, Math.max(1, Math.ceil(need / baseWidth.value * 4) / 4))
})

// distribution view x scale: same as the number line — min left, max right, outliers pinned to the edges
const dx = (g, m, v) => {
  const s = gstat(g, m)
  const lo = s.min
  const hi = s.max === s.min ? s.max + (Math.abs(s.max) * 0.1 || 1) : s.max
  if (v < lo) return 6
  if (v > hi) return 314
  return 16 + 288 * (v - lo) / (hi - lo)
}

// ------------------------------------------------------------- formats
const fmt = (m, v) => {
  if (v == null) return '—'
  if (M(m).unit === '%') return `${(v * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0)}%`
  const a = Math.abs(v)
  return `${v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2)}×`
}
const sideText = (r) => !r ? '' : r.side === 'better' ? 'better than median' : r.side === 'worse' ? 'worse than median' : 'at median'
const cellTitle = (row, m) => {
  if (!row.ev.metrics.includes(m)) return `${M(m).label} does not apply to ${T(row.ticker)?.sector}`
  const r = row.ev.ranks[m]
  if (!r) return flag(row.ticker, m) === 'nm' ? `N/M — ${M(m).nm || 'not meaningful'}` : 'no data'
  const s = gstat(row.group, m)
  return `${M(m).name}: ${fmt(m, val(row.ticker, m))}\n${sideText(r)} (median ${fmt(m, s?.median)}), percentile ${Math.round(r.pct)}`
}
const periodEnd = (p) => {
  const ends = result.value?.period_ends?.[p] || []
  if (!result.value?.coverage[p]) return 'n/a'
  return ends[0] ? ends[0].slice(0, 7) : ''
}
const periodWarning = computed(() => {
  const r = result.value
  if (!r || r.coverage[period.value] >= 0.8 * r.total) return ''
  const label = props.catalog.periods[period.value].label
  return `Only ${r.coverage[period.value]}/${r.total} companies have “${label}” published before ${r.as_of_date} in yfinance ` +
    '(it keeps ~5 quarters and ~4 years). Pick another period or a more recent as_of.'
})

// ------------------------------------------------------------- lock payload
watch([state, category, period, level, threshold, selectedList], () => {
  if (state.value !== 'done') return
  emit('config', {
    task_id: taskId.value, category: category.value, period: period.value, level: level.value,
    threshold: threshold.value, metrics: selectedList.value
  })
})
</script>

<style scoped>
.flab {
  --better: #0F8A5F;
  --better-soft: #E3F5EC;
  --worse: #D13B3B;
  --worse-soft: #FBE9E9;
  max-width: 1440px;
  margin: 0 auto 40px;
  padding: 0 40px;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}
code { font-family: 'JetBrains Mono', monospace; font-size: 0.9em; background: #F0F0F0; padding: 1px 5px; border-radius: 3px; }

.flab-head {
  display: flex; justify-content: space-between; align-items: flex-end; gap: 20px;
  background: #000; color: #FFF; border-radius: 10px 10px 0 0; padding: 22px 24px 20px;
  background-image: radial-gradient(circle at 85% -40%, color-mix(in srgb, var(--cat) 55%, transparent), transparent 60%);
}
.flab-title { display: flex; gap: 14px; align-items: flex-start; }
.block-num {
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 800; width: 26px; height: 26px;
  display: grid; place-items: center; background: #FFF; color: #000; border-radius: 4px; flex-shrink: 0; margin-top: 3px;
}
.flab-title h2 { font-size: 1.7rem; font-weight: 600; letter-spacing: -0.8px; margin: 0 0 4px; }
.flab-title p { margin: 0; font-size: 13px; color: #BBB; max-width: 720px; line-height: 1.55; }
.pill { font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 5px 10px; border-radius: 20px; display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
.pill.ok { background: rgba(15,138,95,0.18); color: #6EE7B7; }
.pill.run { background: rgba(255,255,255,0.1); color: #FFF; }
.pill.idle { background: rgba(255,255,255,0.08); color: #999; }

/* ---- categories ---- */
.cat-row { display: grid; grid-template-columns: repeat(6, 1fr); background: #FFF; border: 1px solid #EAEAEA; border-top: none; }
.cat-card {
  display: flex; flex-direction: column; gap: 3px; text-align: left; padding: 14px 16px 13px; border: none; background: #FFF;
  cursor: pointer; position: relative; font-family: inherit; color: #000; transition: background 0.2s;
}
.cat-card + .cat-card { border-left: 1px solid #EFEFEF; }
.cat-card::after { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: transparent; }
.cat-card.on { background: var(--cs); }
.cat-card.on::after { background: var(--c); }
.cat-name { font-size: 1.05rem; font-weight: 700; letter-spacing: -0.2px; }
.cat-card.on .cat-name { color: var(--c); }
.cat-tag { font-size: 11.5px; color: #777; line-height: 1.35; }
.cat-count { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #AAA; margin-top: 2px; }
.cat-card:not(.on):hover { background: #FAFAFA; }

/* ---- ratio checklist ---- */
.ratio-box, .intro, .loading, .controls, .groups, .shortlist, .lines, .bench, .rank {
  background: #FFF; border: 1px solid #EAEAEA; border-top: none;
}
.ratio-box { padding: 14px 24px 16px; }
.ratio-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 10px; }
.rb-q { display: block; font-weight: 700; font-size: 13.5px; }
.rb-src { display: block; font-size: 11.5px; color: #888; margin-top: 2px; }
.rb-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.all-toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; cursor: pointer; padding: 6px 10px; border: 1px solid #E0E0E0; border-radius: 6px; }
.all-toggle.on { border-color: var(--cat); color: var(--cat); }
.all-toggle input { accent-color: var(--cat); margin: 0; }
.link { border: none; background: none; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; color: #888; cursor: pointer; }
.link:hover { color: #000; }
.formula-btn { border: 1px solid #E0E0E0; background: #FFF; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-size: 11.5px; font-weight: 700; }
.formula-btn.on, .formula-btn:hover { border-color: #000; background: #000; color: #FFF; }
.ratio-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.rchip {
  display: inline-flex; align-items: center; gap: 6px; border: 1px solid #E0E0E0; background: #FFF; border-radius: 20px;
  padding: 5px 11px 5px 6px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; color: #888; transition: all 0.15s;
}
.rchip-box { width: 16px; height: 16px; border-radius: 50%; border: 1.5px solid #CCC; display: grid; place-items: center; font-size: 9px; color: #FFF; }
.rchip.on { border-color: var(--cat); background: var(--cat-soft); color: #000; }
.rchip.on .rchip-box { background: var(--cat); border-color: var(--cat); }
.rchip-dir { font-size: 10px; color: var(--cat); }
.rchip-na { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #B07; font-weight: 600; }
.formulas { margin-top: 14px; border-top: 1px solid #F0F0F0; padding-top: 8px; }
.f-row { display: grid; grid-template-columns: 120px 1fr auto; gap: 2px 12px; padding: 7px 0; border-bottom: 1px dashed #EEE; font-size: 12px; }
.f-label { font-weight: 700; }
.f-formula { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: #333; }
.f-dir { font-size: 11px; color: #999; white-space: nowrap; }
.f-rule { grid-column: 2 / 4; font-size: 11px; color: var(--cat); }
.f-nm { grid-column: 2 / 4; font-size: 11px; color: #B07; }
.f-foot { display: flex; flex-direction: column; gap: 4px; padding: 12px 0 0; font-size: 11.5px; color: #666; }
.fold-enter-active, .fold-leave-active { transition: opacity 0.2s; }
.fold-enter-from, .fold-leave-to { opacity: 0; }

/* ---- intro / loading ---- */
.intro { padding: 22px 24px 24px; border-radius: 0 0 10px 10px; }
.intro-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
.intro-step { font-size: 12.5px; color: #555; line-height: 1.55; border-left: 2px solid #EEE; padding-left: 12px; }
.intro-step span { display: block; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--cat); font-weight: 800; margin-bottom: 2px; }
.intro-step b { display: block; color: #000; font-size: 13.5px; margin-bottom: 3px; }
.intro-cta { display: flex; align-items: center; gap: 16px; }
.load-btn {
  background: #000; color: #FFF; border: none; border-radius: 6px; padding: 14px 22px; cursor: pointer;
  font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 12.5px; letter-spacing: 0.5px;
  display: inline-flex; align-items: center; gap: 10px; transition: background 0.2s;
}
.load-btn:hover:not(:disabled) { background: var(--cat); }
.load-btn:disabled { background: #CCC; cursor: not-allowed; }
.intro-note { font-size: 12px; color: #999; }
.loading { padding: 26px 24px; border-radius: 0 0 10px 10px; }
.load-track { height: 6px; background: #F0F0F0; border-radius: 3px; overflow: hidden; }
.load-fill { height: 100%; background: linear-gradient(90deg, #000, var(--cat)); transition: width 0.4s; }
.load-text { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #666; margin: 12px 0 14px; }
.ticker-rain { display: flex; flex-wrap: wrap; gap: 6px; }
.ticker-rain span { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; padding: 3px 7px; border-radius: 4px; background: #F4F4F4; color: #BBB; transition: all 0.3s; }
.ticker-rain span.done { background: var(--cat-soft); color: var(--cat); }

/* ---- controls ---- */
.controls { display: flex; gap: 22px; align-items: flex-end; padding: 16px 24px; flex-wrap: wrap; background: #FCFCFC; }
.ctl { display: flex; flex-direction: column; gap: 6px; }
.ctl-label { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 700; color: #999; letter-spacing: 0.8px; text-transform: uppercase; }
.seg { display: inline-flex; background: #F0F0F0; border-radius: 7px; padding: 3px; gap: 2px; }
.seg button {
  border: none; background: transparent; padding: 7px 11px; border-radius: 5px; font-size: 12px; font-weight: 600;
  color: #666; cursor: pointer; font-family: inherit; display: inline-flex; flex-direction: column; align-items: center; white-space: nowrap; line-height: 1.2;
}
.seg button i { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #AAA; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
.seg button.on i { color: var(--cat); }
.seg button.empty { opacity: 0.45; }
.seg.small button { padding: 5px 10px; font-size: 11px; text-transform: capitalize; flex-direction: row; }
.zoom { display: flex; align-items: center; gap: 6px; height: 36px; }
.zoom button { width: 26px; height: 26px; border: 1px solid #DDD; background: #FFF; border-radius: 5px; cursor: pointer; font-weight: 700; }
.zoom button.fit { width: auto; padding: 0 8px; font-family: 'JetBrains Mono', monospace; font-size: 10.5px; }
.zoom input { width: 120px; accent-color: #000; }
.show-rm { flex-direction: row; align-items: center; gap: 6px; font-size: 12px; color: #666; height: 36px; cursor: pointer; }
.show-rm input { accent-color: #000; }

.groups { display: flex; gap: 6px; padding: 12px 24px; flex-wrap: wrap; align-items: center; }
.g-label { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 800; color: #999; letter-spacing: 0.8px; text-transform: uppercase; margin-right: 6px; }
.gchip { border: 1px solid #E5E5E5; background: #FFF; border-radius: 6px; padding: 6px 10px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; }
.gchip i { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #AAA; margin-left: 6px; }
.gchip.on { background: #000; color: #FFF; border-color: #000; }
.gchip.static { cursor: default; }

/* ---- shortlist bar ---- */
.shortlist {
  position: sticky; top: 60px; z-index: 5; display: flex; align-items: center; gap: 18px; padding: 10px 24px;
  flex-wrap: wrap; box-shadow: 0 6px 14px -10px rgba(0,0,0,0.25);
}
.sl-count { font-size: 13px; display: flex; align-items: baseline; gap: 6px; }
.sl-count b { font-size: 22px; letter-spacing: -0.5px; }
.sl-count > span { color: #999; }
.sl-target { font-family: 'JetBrains Mono', monospace; font-size: 10px; padding: 2px 7px; border-radius: 10px; background: #F2F2F2; color: #999 !important; }
.sl-target.ok { background: var(--better-soft); color: var(--better) !important; }
.sl-legend { display: flex; gap: 12px; font-size: 11px; color: #666; align-items: center; flex-wrap: wrap; }
.sl-legend span { display: inline-flex; align-items: center; gap: 5px; }
.sl-legend i { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.sl-legend i.dash { height: 0; border-top: 2.5px dashed; background: none !important; }
.sl-legend .tip { color: #AAA; font-style: italic; }
.sl-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rule { font-size: 11.5px; color: #666; }
.rule select { font-family: inherit; font-size: 11.5px; padding: 3px 4px; border: 1px solid #DDD; border-radius: 4px; }
.act { border: 1px solid #E0E0E0; background: #FFF; border-radius: 6px; padding: 7px 11px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; }
.act:hover { border-color: #000; }
.act.primary { background: #000; border-color: #000; color: #FFF; font-family: 'JetBrains Mono', monospace; font-weight: 800; }
.act.primary i { font-style: normal; background: var(--cat); border-radius: 10px; padding: 0 7px; margin-left: 4px; }

/* ---- number lines ---- */
.lines { padding: 16px 24px 20px; display: flex; flex-direction: column; gap: 14px; }
.lcard { border: 1px solid #ECECEC; border-radius: 9px; overflow: hidden; }
.lcard-head { display: flex; align-items: baseline; gap: 10px; padding: 10px 14px; background: #FAFAFA; border-bottom: 1px solid #F0F0F0; }
.lc-label { font-weight: 800; font-size: 15px; }
.lc-name { font-size: 11.5px; color: #888; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lc-dir { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--cat); font-weight: 700; white-space: nowrap; }
.lc-act { border: 1px solid #E5E5E5; background: #FFF; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; font-family: inherit; color: #555; white-space: nowrap; }
.lc-act:hover { border-color: var(--worse); color: var(--worse); }
.lane + .lane { border-top: 1px dashed #EEE; }
.lane-head { display: flex; gap: 10px; align-items: baseline; padding: 8px 14px 0; font-size: 11px; color: #999; font-family: 'JetBrains Mono', monospace; }
.lane-head b { font-family: 'Space Grotesk', sans-serif; font-size: 12.5px; color: #000; }
.nm-list { color: #B07; }
.lane-scroll { overflow-x: auto; padding: 4px 14px 10px; }
.lane-empty { padding: 10px 14px 14px; font-size: 12px; color: #AAA; }
.empty { padding: 20px; text-align: center; color: #AAA; font-size: 12.5px; }

/* ---- distribution ---- */
.bench { padding: 16px 24px 20px; }
.dist-group + .dist-group { margin-top: 18px; }
.dist-title { font-weight: 700; font-size: 13px; margin-bottom: 8px; }
.bench-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 12px; }
.bcard { border: 1px solid #EEE; border-radius: 8px; padding: 12px 14px 10px; }
.bcard-top { display: flex; align-items: baseline; gap: 8px; }
.bcard-label { font-weight: 700; font-size: 14px; }
.bcard-name { font-size: 11.5px; color: #999; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bcard-n { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; }
.strip { width: 100%; height: 70px; display: block; overflow: visible; }
.strip .axis { stroke: #E5E5E5; }
.strip .iqr { fill: #F2F2F2; stroke: #DDD; }
.strip .mine { fill: #334155; stroke: #FFF; stroke-width: 1.5; cursor: pointer; }
.strip .mine.off { fill: #FFF; stroke: #CBD5E1; }
.bstats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; border-top: 1px dashed #EEE; padding-top: 8px; }
.bstats div { display: flex; flex-direction: column; }
.bstats span { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #AAA; text-transform: uppercase; }
.bstats b { font-size: 13px; font-variant-numeric: tabular-nums; }
.bempty { font-size: 12px; color: #AAA; padding: 20px 0; text-align: center; }

/* ---- ranking ---- */
.rank { padding: 20px 24px 18px; border-radius: 0 0 10px 10px; }
.rank-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 14px; }
.eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; color: #999; letter-spacing: 1.4px; margin-bottom: 6px; }
.rank-sub { font-size: 13px; color: #555; }
.rank-sub b { color: var(--cat); font-size: 15px; }
.rtable { overflow: auto; max-height: 560px; border: 1px solid #EEE; border-radius: 8px; }
.r-head, .r-row {
  display: grid; grid-template-columns: 40px minmax(190px, 1.4fr) repeat(var(--cols), minmax(88px, 1fr)) 60px 56px;
  align-items: center; min-width: calc(390px + var(--cols) * 88px);
}
.r-head { background: #FAFAFA; border-bottom: 1px solid #EEE; position: sticky; top: 0; z-index: 2; }
.r-head span { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; color: #999; text-transform: uppercase; padding: 10px 8px; white-space: nowrap; }
.r-head em { font-style: normal; color: var(--cat); }
.sortable { cursor: pointer; user-select: none; }
.sortable.on { color: #000 !important; }
.r-head .r { text-align: right; }
.c { text-align: center; justify-content: center; display: flex; }
.r-row { border-bottom: 1px solid #F3F3F3; cursor: pointer; }
.r-row:hover, .r-row.focus { background: var(--cat-soft); }
.r-row:not(.sel) { opacity: 0.5; }
.r-check { display: grid; place-items: center; }
.check { width: 18px; height: 18px; border-radius: 4px; border: 1.5px solid #CCC; display: grid; place-items: center; font-size: 11px; color: transparent; }
.check.on { background: #000; border-color: #000; color: #FFF; }
.r-company { display: flex; flex-direction: column; padding: 8px; min-width: 0; }
.r-company b { font-family: 'JetBrains Mono', monospace; font-size: 13px; }
.r-row.pass .r-company b::after { content: ' ●'; color: var(--better); font-size: 9px; vertical-align: middle; }
.r-name { font-size: 12px; color: #444; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.r-ind { font-size: 10.5px; color: #AAA; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mcell { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; padding: 8px; }
.mval { font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums; }
.mval sup { font-family: 'JetBrains Mono', monospace; font-size: 7.5px; font-weight: 800; margin-left: 3px; padding: 1px 3px; border-radius: 2px; background: #000; color: #FFF; vertical-align: 4px; }
.mval sup.min { background: #E53935; }
.mbar { width: 100%; max-width: 80px; height: 4px; background: #F0F0F0; border-radius: 2px; overflow: hidden; display: block; }
.mbar i { display: block; height: 100%; background: #BBB; }
.mcell.better .mval { color: var(--better); }
.mcell.better .mbar i { background: var(--better); }
.mcell.worse .mval { color: var(--worse); }
.mcell.worse .mbar i { background: var(--worse); }
.mna { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #BBB; font-style: italic; }
.beats { font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 800; padding: 3px 7px; border-radius: 5px; background: var(--worse-soft); color: var(--worse); }
.beats.ok { background: var(--better-soft); color: var(--better); }
.ring { width: 34px; height: 34px; }
.ring-bg { fill: none; stroke: #EFEFEF; stroke-width: 3.5; }
.ring-fg { fill: none; stroke: var(--cat); stroke-width: 3.5; stroke-linecap: round; transform: rotate(-90deg); transform-origin: 18px 18px; }
.ring text { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; }
.r-empty { padding: 24px; text-align: center; color: #AAA; font-size: 12px; }
.rank-foot { font-size: 11.5px; color: #999; line-height: 1.6; margin: 12px 0 0; }
.better-t { color: var(--better); font-weight: 700; }
.worse-t { color: var(--worse); font-weight: 700; }

.msg { margin: 0; padding: 9px 24px; font-size: 12px; border: 1px solid #EAEAEA; border-top: none; }
.msg.warn { background: #FFF8E6; color: #8A5A00; }
.msg.error { background: #FDECEC; color: #B42318; border: none; padding: 9px 0; margin-top: 10px; }
.spin { width: 11px; height: 11px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #FFF; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 1100px) {
  .cat-row { grid-template-columns: repeat(3, 1fr); }
  .cat-card:nth-child(4) { border-left: none; }
  .cat-card:nth-child(n+4) { border-top: 1px solid #EFEFEF; }
}
@media (max-width: 760px) {
  .flab { padding: 0 16px; }
  .cat-row { grid-template-columns: 1fr 1fr; }
  .intro-grid { grid-template-columns: 1fr 1fr; }
  .shortlist { position: static; }
}
</style>
