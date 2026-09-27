<template>
  <div class="pipeline-home">
    <!-- Navbar -->
    <nav class="navbar">
      <div class="nav-left">
        <div class="nav-brand" @click="router.push('/')">MIROFISH</div>
        <span class="nav-tag">NEWS PIPELINE</span>
      </div>
      <div class="nav-right">
        <a class="nav-link" href="#history" @click.prevent="scrollToHistory">Run history <span class="count-pill">{{ runs.length }}</span></a>
      </div>
    </nav>

    <!-- Hero -->
    <header class="hero">
      <div class="hero-text">
        <div class="eyebrow"><span class="orange-sq"></span> STEP 01 / 08</div>
        <h1 class="hero-title">Build the <span class="accent">ticker universe</span></h1>
        <p class="hero-desc">
          Filter the S&amp;P 500 by GICS sector and market-cap tier at an <code>as_of</code> date.
          The locked universe drives every later step: Finnhub news → reality seed → simulation → consensus → performance.
        </p>
      </div>
      <div class="hero-rail">
        <PipelineStepRail :active="1" />
      </div>
    </header>

    <main class="builder">
      <!-- ============ LEFT: configuration ============ -->
      <section class="config-col">
        <!-- Sectors -->
        <div class="config-block">
          <div class="block-head">
            <span class="block-num">A</span>
            <span class="block-title">GICS Sectors</span>
            <div class="block-actions">
              <button class="link-btn" @click="selectedSectors = [...options.sectors]">All</button>
              <button class="link-btn" @click="selectedSectors = []">Clear</button>
            </div>
          </div>
          <div class="sector-grid">
            <button
              v-for="sector in options.sectors"
              :key="sector"
              class="sector-tile"
              :class="{ on: selectedSectors.includes(sector) }"
              @click="toggle(selectedSectors, sector)"
            >
              <span class="sector-name">{{ sector }}</span>
              <span class="sector-count">{{ sectorCounts[sector] || 0 }}</span>
            </button>
          </div>
        </div>

        <!-- Tiers -->
        <div class="config-block">
          <div class="block-head">
            <span class="block-num">B</span>
            <span class="block-title">Market Cap Tier</span>
            <span class="block-hint" v-if="screenReady">instant — no re-screen</span>
          </div>
          <div class="tier-grid">
            <button
              v-for="tier in options.market_cap_tiers"
              :key="tier.key"
              class="tier-tile"
              :class="[{ on: selectedTiers.includes(tier.key) }, `tier-${tier.key}`]"
              @click="toggle(selectedTiers, tier.key)"
            >
              <span class="tier-swatch"></span>
              <span class="tier-key">{{ tier.key.toUpperCase() }}</span>
              <span class="tier-range">{{ tier.range }}</span>
              <span class="tier-count" v-if="screenReady">{{ tierCounts[tier.key] || 0 }}</span>
            </button>
          </div>
        </div>

        <!-- As-of -->
        <div class="config-block">
          <div class="block-head">
            <span class="block-num">C</span>
            <span class="block-title">As-of Date</span>
          </div>
          <div class="asof-row">
            <input
              type="date"
              class="date-input"
              v-model="asOf"
              :min="options.as_of?.min"
              :max="options.as_of?.max"
            />
            <div class="asof-presets">
              <button v-for="p in asOfPresets" :key="p.label" class="preset-btn" :class="{ on: asOf === p.date }" @click="asOf = p.date">{{ p.label }}</button>
            </div>
          </div>

          <!-- timeline: finnhub range / news window / performance window -->
          <div class="timeline" v-if="timeline">
            <div class="tl-track">
              <div class="tl-seg news" :style="{ left: timeline.newsLeft + '%', width: timeline.newsWidth + '%' }"></div>
              <div class="tl-seg perf" :style="{ left: timeline.asOfPos + '%', width: (100 - timeline.asOfPos) + '%' }"></div>
              <div class="tl-marker" :style="{ left: timeline.asOfPos + '%' }"><span>as_of</span></div>
            </div>
            <div class="tl-labels">
              <span>{{ options.as_of.min }}</span>
              <span>today</span>
            </div>
            <div class="tl-legend">
              <span><i class="lg news"></i> news window {{ timeline.newsStart }} → {{ asOf }}</span>
              <span><i class="lg perf"></i> performance window ({{ timeline.perfDays }}d)</span>
            </div>
          </div>
          <div v-if="asOfError" class="msg error">{{ asOfError }}</div>
          <div v-for="w in asOfWarnings" :key="w" class="msg warn">{{ w }}</div>
        </div>

        <!-- Screen button -->
        <button class="screen-btn" :disabled="!canScreen" @click="runScreen">
          <template v-if="screen.status === 'running'">
            <span class="spinner"></span> SCREENING {{ screen.processed }}/{{ screen.inScope || '…' }}
          </template>
          <template v-else-if="screenReady">RE-SCREEN UNIVERSE ↻</template>
          <template v-else>SCREEN UNIVERSE →</template>
        </button>
        <p class="caveat" v-for="c in options.caveats" :key="c">⚠ {{ c }}</p>
      </section>

      <!-- ============ RIGHT: live universe ============ -->
      <section class="result-col">
        <!-- Stats -->
        <div class="stats-row">
          <div class="stat">
            <div class="stat-value">{{ screenReady || screen.status === 'running' ? universeRows.length : previewRows.length }}</div>
            <div class="stat-label">{{ screenReady || screen.status === 'running' ? 'in ticker_universe' : 'S&P 500 members' }}</div>
          </div>
          <div class="stat">
            <div class="stat-value">{{ formatMarketCap(totalCap) }}</div>
            <div class="stat-label">total market cap</div>
          </div>
          <div class="stat">
            <div class="stat-value">{{ selectedSectors.length }}</div>
            <div class="stat-label">sectors</div>
          </div>
          <div class="stat tier-dist">
            <div class="dist-bar">
              <span
                v-for="tier in tierKeys"
                :key="tier"
                :class="`seg tier-${tier}`"
                :style="{ flexGrow: universeTierCounts[tier] || 0 }"
                :title="`${tier}: ${universeTierCounts[tier] || 0}`"
              ></span>
            </div>
            <div class="stat-label">tier mix · universe</div>
          </div>
        </div>

        <!-- Progress -->
        <div v-if="screen.status === 'running'" class="scan-bar">
          <div class="scan-fill" :style="{ width: screenPct + '%' }"></div>
          <span class="scan-text">Fetching market data · {{ screen.current || 'S&P 500 list' }}</span>
        </div>
        <div v-if="screenStale" class="msg info">Sectors or as_of changed since the last screen — re-screen to refresh market caps.</div>
        <div v-if="screen.status === 'failed'" class="msg error">Screening failed: {{ screen.error }}</div>

        <!-- Toolbar -->
        <div class="toolbar" v-if="selectedSectors.length">
          <div class="tabs">
            <button v-for="t in tabs" :key="t.key" class="tab" :class="{ on: tab === t.key }" @click="tab = t.key">
              {{ t.label }} <span class="tab-count">{{ t.count }}</span>
            </button>
          </div>
          <input class="search" v-model="search" placeholder="Search ticker / company…" />
        </div>

        <!-- Table -->
        <div class="table-wrap" v-if="selectedSectors.length">
          <div class="t-head">
            <span></span>
            <span>Ticker</span>
            <span>Company</span>
            <span>Sector</span>
            <span class="r">Market cap</span>
            <span>Tier</span>
          </div>
          <div class="t-body">
            <div
              v-for="row in visibleRows"
              :key="row.ticker"
              class="t-row"
              :class="row.state"
              @click="row.state === 'in' || row.state === 'excluded' ? toggleExclude(row.ticker) : null"
            >
              <span class="cell-check">
                <span v-if="row.state === 'in' || row.state === 'excluded'" class="check" :class="{ on: row.state === 'in' }">✓</span>
                <span v-else-if="row.state === 'scanning'" class="dot-pulse"></span>
              </span>
              <span class="cell-ticker">{{ row.ticker }}</span>
              <span class="cell-company">{{ row.company_name }}</span>
              <span class="cell-sector">{{ row.gics_sector }}</span>
              <span class="cell-cap r">
                <template v-if="row.market_cap != null">
                  <span class="cap-bar"><span :style="{ width: capWidth(row.market_cap) + '%' }"></span></span>
                  {{ formatMarketCap(row.market_cap) }}
                </template>
                <template v-else>—</template>
              </span>
              <span class="cell-tier">
                <span v-if="row.tier" class="tier-badge" :class="`tier-${row.tier}`">{{ row.tier }}</span>
                <span v-else-if="row.state === 'skipped'" class="state-note" :title="row.reason">no data</span>
                <span v-else class="state-note">{{ row.state === 'preview' ? '—' : 'loading' }}</span>
              </span>
            </div>
          </div>
          <div v-if="!visibleRows.length" class="empty-rows">No tickers in this view.</div>
        </div>

        <div v-else class="empty-state">
          <div class="empty-icon">◇</div>
          <p>Pick one or more sectors — their S&amp;P 500 members appear here instantly.</p>
        </div>

        <!-- Lock bar -->
        <div class="lock-bar" :class="{ ready: canLock }">
          <div class="lock-info">
            <input class="name-input" v-model="runName" :placeholder="defaultRunName" maxlength="80" />
            <span class="lock-sub" v-if="screenReady">
              {{ universeRows.length }} tickers · as_of {{ screen.asOf }} · {{ excluded.size }} excluded by you
            </span>
            <span class="lock-sub" v-else>Screen the universe first to lock it.</span>
          </div>
          <button class="lock-btn" :disabled="!canLock || locking" @click="lockUniverse">
            <span v-if="locking" class="spinner light"></span>
            LOCK UNIVERSE &amp; OPEN WORKSPACE →
          </button>
        </div>
        <div v-if="lockError" class="msg error">{{ lockError }}</div>
      </section>
    </main>

    <!-- ============ Run history ============ -->
    <section class="history" id="history" ref="historyRef">
      <div class="history-head">
        <h2>Run history</h2>
        <span class="history-sub">Every run is saved under <code>backend/uploads/pipeline_runs/</code> — reopen one to continue where it stopped.</span>
      </div>
      <div v-if="!runs.length" class="history-empty">No runs yet.</div>
      <div class="run-grid">
        <div v-for="run in runs" :key="run.run_id" class="run-card" @click="router.push(`/pipeline/${run.run_id}`)">
          <div class="run-top">
            <span class="run-name">{{ run.name }}</span>
            <span class="run-status" :class="run.status">{{ run.status.replace('_', ' ') }}</span>
          </div>
          <div class="run-meta">
            <span>as_of <b>{{ run.config?.as_of_date }}</b></span>
            <span><b>{{ run.steps?.universe?.summary?.ticker_count ?? '—' }}</b> tickers</span>
            <span>{{ (run.config?.market_cap_tiers || []).join(' · ') }}</span>
          </div>
          <div class="run-sectors">{{ (run.config?.sectors || []).join(', ') }}</div>
          <PipelineStepRail :steps="run.steps" compact />
          <div class="run-foot">
            <span>{{ run.run_id }}</span>
            <span class="resume">{{ run.status === 'completed' ? 'Open' : `Resume at step ${run.current_step}` }} →</span>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import PipelineStepRail from '../../components/pipeline/PipelineStepRail.vue'
import { formatMarketCap } from '../../utils/universeGraph'
import {
  getUniverseOptions, validateAsOf, getConstituents,
  startUniverseScreen, getUniverseScreen, createRun, listRuns
} from '../../api/pipeline'

const router = useRouter()
const tierKeys = ['mega', 'large', 'mid', 'small']

// ---- options / inputs ----
const options = ref({ sectors: [], market_cap_tiers: [], as_of: null, caveats: [] })
const allConstituents = ref([])
const selectedSectors = ref([])
const selectedTiers = ref(['mega', 'large'])
const asOf = ref('')
const asOfWarnings = ref([])
const asOfError = ref('')
const runName = ref('')
const runs = ref([])
const historyRef = ref(null)

// ---- screen state ----
// Screens always fetch ALL tiers so tier toggles re-filter instantly; the
// chosen tiers are sent at lock time and the server re-filters.
const screen = reactive({
  taskId: null, status: 'idle', processed: 0, inScope: 0, current: '',
  sectorsKey: '', asOf: '', error: '',
  data: {} // ticker -> {tier, market_cap, outcome: 'screened' | 'skipped', reason}
})
const excluded = ref(new Set())
const tab = ref('universe')
const search = ref('')
const locking = ref(false)
const lockError = ref('')
let pollTimer = null

const isoDaysAgo = (d) => {
  const t = new Date()
  t.setDate(t.getDate() - d)
  return t.toISOString().slice(0, 10)
}
const asOfPresets = [
  { label: '-30d', date: isoDaysAgo(30) },
  { label: '-90d', date: isoDaysAgo(90) },
  { label: '-180d', date: isoDaysAgo(180) },
  { label: '-270d', date: isoDaysAgo(270) }
]

const toggle = (list, value) => {
  const i = list.indexOf(value)
  if (i === -1) list.push(value)
  else list.splice(i, 1)
}

const sectorsKey = computed(() => [...selectedSectors.value].sort().join('|'))
const sectorCounts = computed(() => {
  const counts = {}
  allConstituents.value.forEach(r => { counts[r.gics_sector] = (counts[r.gics_sector] || 0) + 1 })
  return counts
})

const screenStale = computed(() =>
  screen.status === 'done' && (screen.sectorsKey !== sectorsKey.value || screen.asOf !== asOf.value))
const screenReady = computed(() => screen.status === 'done' && !screenStale.value)

const previewRows = computed(() => {
  const set = new Set(selectedSectors.value)
  return allConstituents.value.filter(r => set.has(r.gics_sector))
})

// Merge the instant constituent preview with streamed screen results
const rows = computed(() => {
  const live = screen.status === 'running' || screenReady.value
  return previewRows.value.map(r => {
    const d = live ? screen.data[r.ticker] : null
    let state = 'preview'
    if (screen.status === 'running' && !d) state = 'queued'
    if (d?.outcome === 'skipped') state = 'skipped'
    else if (d) {
      if (!selectedTiers.value.includes(d.tier)) state = 'out'
      else state = excluded.value.has(r.ticker) ? 'excluded' : 'in'
    }
    if (screen.status === 'running' && screen.current === r.ticker && !d) state = 'scanning'
    return { ...r, state, tier: d?.tier || null, market_cap: d?.market_cap ?? null, reason: d?.reason }
  }).sort((a, b) => (b.market_cap ?? -1) - (a.market_cap ?? -1) || a.ticker.localeCompare(b.ticker))
})

const universeRows = computed(() => rows.value.filter(r => r.state === 'in'))
const totalCap = computed(() => (screenReady.value || screen.status === 'running')
  ? universeRows.value.reduce((s, r) => s + r.market_cap, 0) : null)
const countByTier = (list) => list.reduce((c, r) => { c[r.tier] = (c[r.tier] || 0) + 1; return c }, {})
// tiles: every screened ticker per tier (so you see what a toggle would add)
const tierCounts = computed(() => countByTier(rows.value.filter(r => r.tier)))
// distribution bar: only what is in the universe right now
const universeTierCounts = computed(() => countByTier(universeRows.value))
// bar scale follows the rows on screen, so hidden mega caps don't flatten the rest
const maxCap = computed(() => Math.max(1, ...visibleRows.value.map(r => r.market_cap || 0)))
const capWidth = (cap) => Math.max(2, Math.sqrt(cap / maxCap.value) * 100)

// Main "Universe" tab = what will be locked: in-tier rows, rows you excluded
// (kept visible so a click brings them back) and rows whose market cap is
// still loading. Out-of-tier rows leave this tab as soon as their tier is known.
const TAB_STATES = {
  universe: ['in', 'excluded', 'preview', 'queued', 'scanning'],
  out: ['out'],
  skipped: ['skipped']
}
const tabs = computed(() => {
  const count = (states) => rows.value.filter(r => states.includes(r.state)).length
  return [
    { key: 'universe', label: 'Universe', count: count(['in']) },
    { key: 'out', label: 'Out of tier', count: count(TAB_STATES.out) },
    { key: 'skipped', label: 'No data', count: count(TAB_STATES.skipped) },
    { key: 'all', label: 'All', count: rows.value.length }
  ]
})

const visibleRows = computed(() => {
  const q = search.value.trim().toLowerCase()
  return rows.value.filter(r => {
    if (TAB_STATES[tab.value] && !TAB_STATES[tab.value].includes(r.state)) return false
    return !q || r.ticker.toLowerCase().includes(q) || r.company_name.toLowerCase().includes(q)
  })
})

const screenPct = computed(() => screen.inScope ? Math.round(screen.processed * 100 / screen.inScope) : 3)

const canScreen = computed(() =>
  selectedSectors.value.length > 0 && asOf.value && !asOfError.value && screen.status !== 'running')
const canLock = computed(() =>
  screenReady.value && universeRows.value.length > 0 && selectedTiers.value.length > 0)

const defaultRunName = computed(() => {
  const s = selectedSectors.value.length > 2 ? `${selectedSectors.value.length} sectors` : selectedSectors.value.join(' + ')
  return `${s || 'Universe'} · ${selectedTiers.value.join('/')} · ${asOf.value}`
})

// ---- as_of timeline (range = Finnhub limit .. today) ----
const timeline = computed(() => {
  const a = options.value.as_of
  if (!a || !asOf.value) return null
  const min = new Date(a.min).getTime()
  const max = new Date(a.max).getTime()
  const at = new Date(asOf.value).getTime()
  if (isNaN(at)) return null
  const span = max - min
  const day = 86400000
  const pos = (t) => Math.min(100, Math.max(0, (t - min) / span * 100))
  const newsStart = at - a.news_window_days * day
  return {
    asOfPos: pos(at),
    newsLeft: pos(newsStart),
    newsWidth: pos(at) - pos(newsStart),
    newsStart: new Date(newsStart).toISOString().slice(0, 10),
    perfDays: Math.round((max - at) / day)
  }
})

watch(asOf, async (value) => {
  asOfError.value = ''
  asOfWarnings.value = []
  if (!value) return
  try {
    const res = await validateAsOf(value)
    asOfWarnings.value = res.data.warnings
  } catch (e) {
    asOfError.value = e.message
  }
})

// ---- screening ----
const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const absorb = (rowsIn, outcome) => {
  (rowsIn || []).forEach(r => {
    screen.data[r.ticker] = outcome === 'skipped'
      ? { outcome, reason: r.reason }
      : { outcome, tier: r.market_cap_tier, market_cap: r.market_cap }
  })
}

const pollScreen = async () => {
  try {
    const { data: task } = await getUniverseScreen(screen.taskId)
    const d = task.progress_detail || {}
    screen.processed = d.processed || screen.processed
    screen.inScope = d.in_scope || screen.inScope
    screen.current = d.current || ''
    absorb(d.passed, 'screened')
    absorb(d.filtered, 'screened')
    absorb(d.skipped, 'skipped')
    if (task.status === 'completed') {
      absorb(task.result.universe, 'screened')
      absorb(task.result.filtered_out, 'screened')
      absorb(task.result.skipped, 'skipped')
      screen.status = 'done'
      screen.current = ''
      stopPolling()
    } else if (task.status === 'failed') {
      screen.status = 'failed'
      screen.error = task.error
      stopPolling()
    }
  } catch (e) {
    screen.status = 'failed'
    screen.error = e.message
    stopPolling()
  }
}

const runScreen = async () => {
  stopPolling()
  lockError.value = ''
  Object.assign(screen, {
    status: 'running', processed: 0, inScope: previewRows.value.length, current: '',
    sectorsKey: sectorsKey.value, asOf: asOf.value, error: '', data: {}
  })
  excluded.value = new Set()
  try {
    const res = await startUniverseScreen({
      sectors: selectedSectors.value, market_cap_tiers: tierKeys, as_of_date: asOf.value
    })
    screen.taskId = res.data.task_id
    pollTimer = setInterval(pollScreen, 800)
  } catch (e) {
    screen.status = 'failed'
    screen.error = e.message
  }
}

const toggleExclude = (ticker) => {
  const next = new Set(excluded.value)
  next.has(ticker) ? next.delete(ticker) : next.add(ticker)
  excluded.value = next
}

const lockUniverse = async () => {
  locking.value = true
  lockError.value = ''
  try {
    const res = await createRun({
      task_id: screen.taskId,
      name: runName.value.trim() || defaultRunName.value,
      market_cap_tiers: selectedTiers.value,
      excluded_tickers: [...excluded.value]
    })
    router.push(`/pipeline/${res.data.run_id}`)
  } catch (e) {
    lockError.value = e.message
  } finally {
    locking.value = false
  }
}

// Auto-screen: market cap (and therefore the tier filter) is only known after
// a screen, so start one as soon as sectors / as_of settle. Tier toggles never
// need a re-screen (every tier is fetched).
let autoTimer = null
const scheduleAutoScreen = () => {
  clearTimeout(autoTimer)
  autoTimer = setTimeout(() => {
    if (!selectedSectors.value.length || !asOf.value || asOfError.value) return
    const alreadyScreened = screen.sectorsKey === sectorsKey.value && screen.asOf === asOf.value &&
      (screen.status === 'running' || screen.status === 'done')
    if (!alreadyScreened) runScreen()
  }, 700)
}
watch([sectorsKey, asOf, asOfError], () => {
  if (!selectedSectors.value.length) {
    clearTimeout(autoTimer)
    stopPolling()
    if (screen.status === 'running') screen.status = 'idle'
    return
  }
  scheduleAutoScreen()
})

const scrollToHistory = () => historyRef.value?.scrollIntoView({ behavior: 'smooth' })

onMounted(async () => {
  try {
    const [opt, cons, runList] = await Promise.all([getUniverseOptions(), getConstituents(), listRuns()])
    options.value = opt.data
    allConstituents.value = cons.data
    runs.value = runList.data
    asOf.value = isoDaysAgo(90)
  } catch (e) {
    lockError.value = `Backend unreachable: ${e.message}`
  }
})

onUnmounted(() => {
  stopPolling()
  clearTimeout(autoTimer)
})
</script>

<style scoped>
.pipeline-home {
  min-height: 100vh;
  background: #FAFAFA;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
  color: #000;
  background-image:
    linear-gradient(rgba(0,0,0,0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,0,0,0.035) 1px, transparent 1px);
  background-size: 32px 32px;
}
code {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9em;
  background: #F0F0F0;
  padding: 1px 5px;
  border-radius: 3px;
}

/* ---- navbar ---- */
.navbar {
  height: 60px;
  background: #000;
  color: #FFF;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 40px;
  position: sticky;
  top: 0;
  z-index: 50;
}
.nav-left { display: flex; align-items: center; gap: 14px; }
.nav-brand {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  letter-spacing: 1px;
  font-size: 1.2rem;
  cursor: pointer;
}
.nav-tag {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 700;
  background: #FF5722;
  padding: 3px 8px;
  letter-spacing: 1px;
}
.nav-link {
  color: #FFF;
  text-decoration: none;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  opacity: 0.85;
}
.nav-link:hover { opacity: 1; }
.count-pill {
  background: #333;
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 11px;
}

/* ---- hero ---- */
.hero {
  max-width: 1440px;
  margin: 0 auto;
  padding: 40px 40px 28px;
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 48px;
  align-items: end;
}
.eyebrow {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  color: #999;
  letter-spacing: 1.5px;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.orange-sq { width: 8px; height: 8px; background: #FF5722; display: inline-block; }
.hero-title {
  font-size: 2.6rem;
  font-weight: 600;
  letter-spacing: -1.5px;
  line-height: 1.1;
  margin-bottom: 14px;
}
.accent {
  background: linear-gradient(90deg, #000 0%, #FF5722 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.hero-desc { font-size: 14px; line-height: 1.7; color: #555; max-width: 640px; }
.hero-rail {
  background: #FFF;
  border: 1px solid #EAEAEA;
  padding: 16px 18px;
  border-radius: 8px;
  box-shadow: 0 2px 10px rgba(0,0,0,0.03);
}

/* ---- builder grid ---- */
.builder {
  max-width: 1440px;
  margin: 0 auto;
  padding: 0 40px 40px;
  display: grid;
  grid-template-columns: 400px 1fr;
  gap: 24px;
  align-items: start;
}
.config-col {
  position: sticky;
  top: 80px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.config-block {
  background: #FFF;
  border: 1px solid #EAEAEA;
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.03);
}
.block-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.block-num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 800;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  background: #000;
  color: #FFF;
  border-radius: 3px;
}
.block-title { font-weight: 700; font-size: 13px; letter-spacing: 0.3px; }
.block-hint {
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #FF5722;
}
.block-actions { margin-left: auto; display: flex; gap: 10px; }
.link-btn {
  border: none;
  background: none;
  font-size: 11px;
  font-weight: 600;
  color: #999;
  cursor: pointer;
  font-family: 'JetBrains Mono', monospace;
}
.link-btn:hover { color: #000; }

.sector-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.sector-tile {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  border: 1px solid #E5E5E5;
  background: #FFF;
  border-radius: 5px;
  padding: 8px 10px;
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
  transition: all 0.15s;
}
.sector-tile:hover { border-color: #999; }
.sector-tile.on { background: #000; border-color: #000; color: #FFF; }
.sector-name { font-weight: 600; line-height: 1.25; }
.sector-count {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #AAA;
}
.sector-tile.on .sector-count { color: #FF5722; }

.tier-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
}
.tier-tile {
  border: 1px solid #E5E5E5;
  background: #FFF;
  border-radius: 5px;
  padding: 10px 8px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  cursor: pointer;
  position: relative;
  transition: all 0.15s;
}
.tier-tile:hover { border-color: #999; }
.tier-tile.on { border-color: #000; box-shadow: inset 0 0 0 1px #000; }
.tier-swatch { width: 18px; height: 4px; border-radius: 2px; margin-bottom: 4px; }
.tier-key { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 12px; }
.tier-range { font-size: 10px; color: #888; }
.tier-count {
  position: absolute;
  top: 8px;
  right: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #999;
}
.tier-tile:not(.on) { opacity: 0.55; }

/* tier palette */
.tier-mega .tier-swatch, .seg.tier-mega { background: #000; }
.tier-large .tier-swatch, .seg.tier-large { background: #FF5722; }
.tier-mid .tier-swatch, .seg.tier-mid { background: #FFAB91; }
.tier-small .tier-swatch, .seg.tier-small { background: #BDBDBD; }

.asof-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.date-input {
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  padding: 7px 10px;
  border: 1px solid #DDD;
  border-radius: 5px;
  outline: none;
}
.date-input:focus { border-color: #000; }
.asof-presets { display: flex; gap: 4px; }
.preset-btn {
  border: 1px solid #E5E5E5;
  background: #FFF;
  border-radius: 4px;
  padding: 6px 7px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10.5px;
  cursor: pointer;
  color: #666;
}
.preset-btn.on { border-color: #000; color: #000; font-weight: 700; }

.timeline { margin-top: 16px; }
.tl-track {
  position: relative;
  height: 10px;
  background: repeating-linear-gradient(90deg, #F2F2F2 0 6px, #EAEAEA 6px 7px);
  border-radius: 5px;
}
.tl-seg { position: absolute; top: 0; bottom: 0; border-radius: 5px; transition: all 0.3s; }
.tl-seg.news { background: #FF5722; }
.tl-seg.perf { background: repeating-linear-gradient(135deg, #4CAF50 0 4px, #A5D6A7 4px 8px); opacity: 0.8; }
.tl-marker {
  position: absolute;
  top: -5px;
  width: 2px;
  height: 20px;
  background: #000;
  transition: left 0.3s;
}
.tl-marker span {
  position: absolute;
  top: -16px;
  left: 50%;
  transform: translateX(-50%);
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  font-weight: 700;
  white-space: nowrap;
}
.tl-labels {
  display: flex;
  justify-content: space-between;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5px;
  color: #AAA;
  margin-top: 6px;
}
.tl-legend {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-top: 8px;
  font-size: 10.5px;
  color: #666;
  font-family: 'JetBrains Mono', monospace;
}
.lg { display: inline-block; width: 10px; height: 6px; border-radius: 2px; margin-right: 4px; }
.lg.news { background: #FF5722; }
.lg.perf { background: #66BB6A; }

.msg {
  margin-top: 10px;
  font-size: 11.5px;
  line-height: 1.5;
  padding: 8px 10px;
  border-radius: 5px;
}
.msg.error { background: #FFEBEE; color: #C62828; border-left: 3px solid #F44336; }
.msg.warn { background: #FFF8E1; color: #8D6E00; border-left: 3px solid #FFB300; }
.msg.info { background: #F3F3F3; color: #555; border-left: 3px solid #999; margin-top: 0; margin-bottom: 12px; }

.screen-btn {
  background: #000;
  color: #FFF;
  border: none;
  padding: 16px;
  border-radius: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 1px;
  cursor: pointer;
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 10px;
  transition: all 0.2s;
}
.screen-btn:hover:not(:disabled) { background: #FF5722; transform: translateY(-1px); box-shadow: 0 6px 18px rgba(255,87,34,0.25); }
.screen-btn:disabled { background: #CCC; cursor: not-allowed; }
.caveat { font-size: 10.5px; color: #999; line-height: 1.5; }

/* ---- results ---- */
.result-col {
  background: #FFF;
  border: 1px solid #EAEAEA;
  border-radius: 8px;
  box-shadow: 0 2px 10px rgba(0,0,0,0.03);
  padding: 20px;
  display: flex;
  flex-direction: column;
  min-height: 640px;
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr) 1.4fr;
  gap: 12px;
  margin-bottom: 16px;
}
.stat {
  border: 1px solid #F0F0F0;
  border-radius: 6px;
  padding: 12px 14px;
  background: #FCFCFC;
}
.stat-value {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 22px;
  letter-spacing: -0.5px;
}
.stat-label { font-size: 10.5px; color: #999; margin-top: 2px; text-transform: uppercase; letter-spacing: 0.5px; }
.dist-bar {
  display: flex;
  height: 12px;
  margin: 8px 0 8px;
  border-radius: 3px;
  overflow: hidden;
  background: #F0F0F0;
  gap: 1px;
}
.seg { transition: flex-grow 0.4s; }

.scan-bar {
  position: relative;
  height: 26px;
  background: #F5F5F5;
  border-radius: 5px;
  overflow: hidden;
  margin-bottom: 12px;
}
.scan-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #FF5722, #FF8A65);
  transition: width 0.5s;
}
.scan-fill::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.5), transparent);
  animation: shimmer 1.2s infinite;
}
.scan-text {
  position: relative;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  line-height: 26px;
  padding-left: 10px;
  color: #000;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.tabs { display: flex; gap: 4px; background: #F5F5F5; padding: 3px; border-radius: 6px; }
.tab {
  border: none;
  background: transparent;
  padding: 6px 10px;
  font-size: 11.5px;
  font-weight: 600;
  color: #777;
  border-radius: 4px;
  cursor: pointer;
}
.tab.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.tab-count { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #FF5722; margin-left: 3px; }
.search {
  border: 1px solid #E5E5E5;
  border-radius: 5px;
  padding: 7px 10px;
  font-size: 12px;
  width: 220px;
  outline: none;
  font-family: inherit;
}
.search:focus { border-color: #000; }

.table-wrap {
  flex: 1;
  border: 1px solid #F0F0F0;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.t-head, .t-row {
  display: grid;
  grid-template-columns: 34px 72px minmax(160px, 1.6fr) minmax(120px, 1fr) 170px 76px;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
}
.t-head {
  height: 34px;
  background: #FAFAFA;
  border-bottom: 1px solid #EEE;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 700;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.t-body { max-height: 520px; overflow-y: auto; position: relative; }
.t-row {
  height: 40px;
  border-bottom: 1px solid #F5F5F5;
  font-size: 12.5px;
  transition: background 0.15s, opacity 0.2s;
}
.t-row.in, .t-row.excluded { cursor: pointer; }
.t-row.in:hover, .t-row.excluded:hover { background: #FFF6F3; }
.t-row.out, .t-row.excluded { opacity: 0.42; }
.t-row.skipped { opacity: 0.5; }
.t-row.queued, .t-row.preview { color: #777; }
.t-row.scanning { background: #FFF3EE; }
.r { text-align: right; justify-content: flex-end; }
.cell-ticker { font-family: 'JetBrains Mono', monospace; font-weight: 800; }
.cell-company, .cell-sector { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cell-sector { color: #888; font-size: 11.5px; }
.cell-cap {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}
.cap-bar { width: 70px; height: 5px; background: #F2F2F2; border-radius: 3px; overflow: hidden; display: flex; justify-content: flex-end; }
.cap-bar span { background: #000; height: 100%; border-radius: 3px; transition: width 0.4s; }
.t-row.in .cap-bar span { background: #FF5722; }
.check {
  width: 16px;
  height: 16px;
  border: 1.5px solid #CCC;
  border-radius: 3px;
  display: grid;
  place-items: center;
  font-size: 10px;
  color: transparent;
}
.check.on { background: #000; border-color: #000; color: #FFF; }
.dot-pulse {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #FF5722;
  animation: pulse 0.8s infinite;
}
.tier-badge {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5px;
  font-weight: 800;
  padding: 3px 7px;
  border-radius: 3px;
  text-transform: uppercase;
  color: #FFF;
}
.tier-badge.tier-mega { background: #000; }
.tier-badge.tier-large { background: #FF5722; }
.tier-badge.tier-mid { background: #FFAB91; color: #5D2A1A; }
.tier-badge.tier-small { background: #E0E0E0; color: #555; }
.state-note { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #BBB; }
.empty-rows { padding: 30px; text-align: center; color: #AAA; font-size: 12px; }

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #AAA;
  gap: 10px;
  font-size: 13px;
  border: 1px dashed #E5E5E5;
  border-radius: 6px;
}
.empty-icon { font-size: 34px; color: #DDD; }

.lock-bar {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 16px;
  border-radius: 8px;
  background: #F5F5F5;
  transition: all 0.3s;
}
.lock-bar.ready { background: #000; }
.lock-info { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.name-input {
  border: none;
  background: transparent;
  font-size: 15px;
  font-weight: 700;
  outline: none;
  font-family: inherit;
  color: #000;
}
.lock-bar.ready .name-input { color: #FFF; }
.name-input::placeholder { color: #999; }
.lock-sub { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #999; }
.lock-btn {
  background: #FF5722;
  color: #FFF;
  border: none;
  padding: 13px 20px;
  border-radius: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 12px;
  letter-spacing: 0.8px;
  cursor: pointer;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: transform 0.15s, box-shadow 0.15s;
}
.lock-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(255,87,34,0.4); }
.lock-btn:disabled { background: #D5D5D5; cursor: not-allowed; }

/* ---- history ---- */
.history {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px 40px 80px;
}
.history-head { display: flex; align-items: baseline; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.history-head h2 { font-size: 20px; font-weight: 600; letter-spacing: -0.5px; }
.history-sub { font-size: 12px; color: #888; }
.history-empty { color: #AAA; font-size: 13px; }
.run-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}
.run-card {
  background: #FFF;
  border: 1px solid #EAEAEA;
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: pointer;
  transition: all 0.2s;
}
.run-card:hover { border-color: #000; transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,0.06); }
.run-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.run-name { font-weight: 700; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-status {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5px;
  font-weight: 800;
  text-transform: uppercase;
  padding: 3px 7px;
  border-radius: 3px;
  background: #FFF3EE;
  color: #FF5722;
  white-space: nowrap;
}
.run-status.completed { background: #E8F5E9; color: #2E7D32; }
.run-status.failed { background: #FFEBEE; color: #C62828; }
.run-meta { display: flex; gap: 12px; font-size: 11.5px; color: #666; font-family: 'JetBrains Mono', monospace; flex-wrap: wrap; }
.run-meta b { color: #000; }
.run-sectors { font-size: 11.5px; color: #999; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-foot {
  display: flex;
  justify-content: space-between;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: #BBB;
}
.resume { color: #000; font-weight: 700; }

/* ---- misc ---- */
.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid rgba(255,255,255,0.35);
  border-top-color: #FFF;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
/* a row flashes when its market data lands (class flips queued -> in/out) */
.t-row.in, .t-row.out, .t-row.skipped { animation: landed 0.6s ease-out; }
@keyframes landed { from { background: #FFE0D4; } to { background: transparent; } }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 50% { opacity: 0.3; } }
@keyframes shimmer { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

@media (max-width: 1100px) {
  .hero, .builder { grid-template-columns: 1fr; }
  .config-col { position: static; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
