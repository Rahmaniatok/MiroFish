<template>
  <section class="final">
    <div class="final-head">
      <div class="fh-title">
        <span class="block-num">G</span>
        <div>
          <h2>Final list <span class="count">{{ rows.length }}</span></h2>
          <p>Every company that made it through the filters — this is the list the news and the swarm agents will work on.</p>
        </div>
      </div>
      <div class="fh-stats" v-if="rows.length">
        <div class="fs"><b>{{ formatMarketCap(totalCap) }}</b><span>total market cap</span></div>
        <div class="fs"><b>{{ sectorCount }}</b><span>sector{{ sectorCount === 1 ? '' : 's' }}</span></div>
        <div class="fs"><b>{{ industryCount }}</b><span>industr{{ industryCount === 1 ? 'y' : 'ies' }}</span></div>
        <div class="fs strat"><b :style="{ color: strategyColor }">{{ strategyLabel }}</b><span>strategy</span></div>
      </div>
    </div>

    <div v-if="!ready" class="final-empty">Screen the universe first — the final list appears here.</div>
    <div v-else-if="!rows.length" class="final-empty">No company left — restore some in the table above or in the Fundamental Lab.</div>

    <template v-else>
      <div class="toolbar">
        <input class="search" v-model="search" placeholder="Search company / ticker / industry…" />
        <span class="tb-note">
          Prices: close on <b>as_of {{ asOf }}</b><template v-if="showToday"> · <span class="today-k">today</span> for reference only</template>
          · 52-week range ends on as_of
        </span>
        <button class="tb-btn" @click="downloadCsv">↓ CSV</button>
      </div>

      <div class="ftable">
        <div class="f-row f-head" :class="{ today: showToday }">
          <span>#</span>
          <span v-for="c in columns" :key="c.key" :class="[c.align, { sortable: c.sort, on: sortKey === c.key }]"
                @click="c.sort && sortBy(c.key)">
            {{ c.label }}<em v-if="sortKey === c.key">{{ sortDir === 1 ? '▲' : '▼' }}</em>
          </span>
          <span></span>
        </div>
        <div v-for="(r, i) in sortedRows" :key="r.ticker" class="f-row" :class="{ today: showToday }">
          <span class="idx">{{ i + 1 }}</span>
          <span class="company">
            <b>{{ r.company_name }}</b>
            <span class="tick">{{ r.ticker }}</span>
          </span>
          <span class="sector" :title="r.gics_sector"><i :style="{ background: sectorColor(r.gics_sector) }"></i><em>{{ r.gics_sector }}</em></span>
          <span class="industry" :title="r.gics_sub_industry">{{ r.gics_sub_industry || '—' }}</span>
          <span class="r cap">
            {{ formatMarketCap(r.market_cap) }}
            <span class="tier" :class="`tier-${r.tier}`">{{ r.tier }}</span>
          </span>
          <span class="strategy">
            <template v-if="strategy">
              <span class="s-pill" :style="{ background: strategyColor }">{{ strategyLabel }}</span>
              <template v-if="evalOf(r.ticker)">
                <span class="s-pass" :class="{ ok: evalOf(r.ticker).passes }" :title="`beats the median on ${evalOf(r.ticker).beat_count} of ${evalOf(r.ticker).n} ratios`">
                  {{ evalOf(r.ticker).passes ? '✓' : '✗' }} {{ evalOf(r.ticker).beat_count }}/{{ evalOf(r.ticker).n }}
                </span>
                <span class="s-score" v-if="evalOf(r.ticker).score != null" title="average industry percentile">{{ Math.round(evalOf(r.ticker).score) }}</span>
              </template>
            </template>
            <span v-else class="none">no fundamental filter</span>
          </span>
          <span class="r price">{{ money(r.price?.close) }}</span>
          <span v-if="showToday" class="r price today-v">
            {{ money(quotes[r.ticker]?.close) }}
            <i v-if="changeSince(r) != null" :class="changeSince(r) >= 0 ? 'up' : 'down'">{{ pct(changeSince(r), true) }}</i>
          </span>
          <span class="r">{{ money(r.price?.high_52w) }}</span>
          <span class="r">{{ money(r.price?.low_52w) }}</span>
          <span class="range" :title="rangeTitle(r)">
            <template v-if="rangePos(r) != null">
              <span class="rg-track">
                <span class="rg-fill" :style="{ width: rangePos(r) + '%' }"></span>
                <span class="rg-dot" :style="{ left: rangePos(r) + '%' }"></span>
                <span v-if="showToday && todayPos(r) != null" class="rg-dot today" :style="{ left: todayPos(r) + '%' }"></span>
              </span>
              <span class="rg-off">{{ pct(offHigh(r)) }} vs high</span>
            </template>
            <span v-else class="none">—</span>
          </span>
          <span class="rm"><button @click="$emit('remove', r.ticker)" title="Remove from the list">✕</button></span>
        </div>
      </div>

      <div class="final-foot">
        <span>{{ rows.length }} companies · as_of {{ asOf }}<template v-if="strategy"> · {{ strategyLabel }} ratios, {{ passingCount }} pass</template></span>
        <button class="lock-btn" :disabled="!canLock || locking" @click="$emit('lock')">
          <span v-if="locking" class="spin"></span>
          LOCK THIS LIST &amp; FETCH NEWS →
        </button>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { formatMarketCap } from '../../utils/universeGraph'
import { getQuotes } from '../../api/pipeline'
import { sectorColor } from '../../utils/sectorColors'

const props = defineProps({
  rows: { type: Array, default: () => [] },       // kept universe rows (PipelineHome universeRows)
  ready: { type: Boolean, default: false },
  asOf: { type: String, default: '' },
  strategy: { type: Object, default: null },      // Fundamental Lab config {category, ...} or null
  evals: { type: Object, default: () => ({}) },   // ticker -> {passes, beat_count, n, score}
  catalog: { type: Object, default: null },
  canLock: { type: Boolean, default: false },
  locking: { type: Boolean, default: false }
})
defineEmits(['remove', 'lock'])

const CAT_COLORS = { value: '#6D28D9', growth: '#FF5722', dividend: '#0E7490', quality: '#B45309', garp: '#BE185D', defensive: '#475569' }

const strategyLabel = computed(() => props.strategy
  ? (props.catalog?.categories?.[props.strategy.category]?.label || props.strategy.category) : '—')
const strategyColor = computed(() => CAT_COLORS[props.strategy?.category] || '#999')
const evalOf = (t) => props.evals?.[t] || null
const passingCount = computed(() => props.rows.filter(r => evalOf(r.ticker)?.passes).length)

const totalCap = computed(() => props.rows.reduce((s, r) => s + (r.market_cap || 0), 0))
const sectorCount = computed(() => new Set(props.rows.map(r => r.gics_sector)).size)
const industryCount = computed(() => new Set(props.rows.map(r => r.gics_sub_industry)).size)

// ---- today's price (reference only) when as_of is in the past ----
const quotes = ref({})
const isoToday = () => new Date().toISOString().slice(0, 10)
const showToday = computed(() => props.asOf && props.asOf < new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10))
let quoteTimer = null
const fetchMissing = () => {
  clearTimeout(quoteTimer)
  quoteTimer = setTimeout(async () => {
    const missing = props.rows.map(r => r.ticker).filter(t => !(t in quotes.value))
    if (!missing.length) return
    try {
      const res = await getQuotes(missing)
      const next = { ...quotes.value }
      missing.forEach(t => { next[t] = res.data[t] || null })
      quotes.value = next
    } catch (e) {
      // display-only column: leave it empty on failure
    }
  }, 500)
}
watch(() => [showToday.value, props.rows.map(r => r.ticker).join(',')], () => { if (showToday.value) fetchMissing() }, { immediate: true })
watch(() => props.asOf, () => { quotes.value = {} })

// ---- columns / sorting ----
const columns = computed(() => [
  { key: 'company', label: 'Company', sort: true },
  { key: 'sector', label: 'Sector', sort: true },
  { key: 'industry', label: 'Industry', sort: true },
  { key: 'cap', label: 'Market cap', sort: true, align: 'r' },
  { key: 'strategy', label: 'Strategy', sort: true },
  { key: 'price', label: 'Price (as_of)', sort: true, align: 'r' },
  ...(showToday.value ? [{ key: 'today', label: `Today`, sort: true, align: 'r' }] : []),
  { key: 'high', label: '52W high', sort: true, align: 'r' },
  { key: 'low', label: '52W low', sort: true, align: 'r' },
  { key: 'range', label: 'Position in 52W range', sort: true }
])
const search = ref('')
const sortKey = ref('cap')
const sortDir = ref(-1)
const sortBy = (k) => {
  if (sortKey.value === k) sortDir.value *= -1
  else { sortKey.value = k; sortDir.value = ['company', 'sector', 'industry'].includes(k) ? 1 : -1 }
}
const keyOf = (r, k) => ({
  company: r.company_name, sector: r.gics_sector, industry: r.gics_sub_industry || '', cap: r.market_cap ?? -1,
  strategy: evalOf(r.ticker)?.score ?? -1, price: r.price?.close ?? -1, today: quotes.value[r.ticker]?.close ?? -1,
  high: r.price?.high_52w ?? -1, low: r.price?.low_52w ?? -1, range: rangePos(r) ?? -1
}[k])
const sortedRows = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = props.rows.filter(r => !q || [r.ticker, r.company_name, r.gics_sub_industry, r.gics_sector]
    .some(x => (x || '').toLowerCase().includes(q)))
  return [...list].sort((a, b) => {
    const x = keyOf(a, sortKey.value)
    const y = keyOf(b, sortKey.value)
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir.value
  })
})

// ---- 52-week range ----
const posIn = (r, v) => {
  const lo = r.price?.low_52w
  const hi = r.price?.high_52w
  if (v == null || lo == null || hi == null || hi <= lo) return null
  return Math.min(100, Math.max(0, (v - lo) / (hi - lo) * 100))
}
const rangePos = (r) => posIn(r, r.price?.close)
const todayPos = (r) => posIn(r, quotes.value[r.ticker]?.close)
const offHigh = (r) => (r.price?.close != null && r.price?.high_52w) ? r.price.close / r.price.high_52w - 1 : null
const changeSince = (r) => (quotes.value[r.ticker]?.close != null && r.price?.close) ? quotes.value[r.ticker].close / r.price.close - 1 : null
const rangeTitle = (r) => `52W low ${money(r.price?.low_52w)} · as_of ${money(r.price?.close)} · high ${money(r.price?.high_52w)}` +
  (quotes.value[r.ticker] ? ` · today ${money(quotes.value[r.ticker].close)}` : '')

// ---- formats ----
const money = (v) => v == null ? '—' : `$${v >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 0 }) : v.toFixed(2)}`
const pct = (v, sign = false) => v == null ? '—' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

const downloadCsv = () => {
  const head = ['Company', 'Ticker', 'Sector', 'Industry', 'Market cap', 'Tier', 'Strategy', 'Passes', 'Beats', 'Score',
    `Price (as_of ${props.asOf})`, ...(showToday.value ? [`Price (today ${isoToday()})`] : []), '52W high', '52W low', 'vs 52W high']
  const esc = (v) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = sortedRows.value.map(r => {
    const e = evalOf(r.ticker)
    return [r.company_name, r.ticker, r.gics_sector, r.gics_sub_industry, r.market_cap, r.tier,
      props.strategy ? strategyLabel.value : '', e ? (e.passes ? 'yes' : 'no') : '', e ? `${e.beat_count}/${e.n}` : '',
      e?.score != null ? Math.round(e.score) : '', r.price?.close,
      ...(showToday.value ? [quotes.value[r.ticker]?.close] : []), r.price?.high_52w, r.price?.low_52w,
      offHigh(r) != null ? (offHigh(r) * 100).toFixed(1) + '%' : ''].map(v => typeof v === 'number' && !Number.isInteger(v) ? +v.toFixed(2) : v).map(esc).join(',')
  })
  const blob = new Blob([[head.join(','), ...lines].join('\n')], { type: 'text/csv' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `final_list_${props.asOf}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<style scoped>
.final {
  max-width: 1440px; margin: 0 auto 40px; padding: 0 40px;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}
.final-head {
  display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; flex-wrap: wrap;
  background: #FFF; border: 1px solid #EAEAEA; border-radius: 10px 10px 0 0; padding: 20px 24px;
  border-top: 3px solid #000;
}
.fh-title { display: flex; gap: 14px; align-items: flex-start; }
.block-num {
  font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 800; width: 26px; height: 26px;
  display: grid; place-items: center; background: #000; color: #FFF; border-radius: 4px; flex-shrink: 0; margin-top: 3px;
}
.fh-title h2 { font-size: 1.7rem; font-weight: 600; letter-spacing: -0.8px; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }
.count { font-family: 'JetBrains Mono', monospace; font-size: 13px; background: #FF5722; color: #FFF; padding: 2px 9px; border-radius: 12px; letter-spacing: 0; }
.fh-title p { margin: 0; font-size: 13px; color: #777; }
.fh-stats { display: flex; gap: 26px; }
.fs { display: flex; flex-direction: column; }
.fs b { font-size: 1.25rem; letter-spacing: -0.4px; }
.fs span { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #999; text-transform: uppercase; letter-spacing: 0.6px; }

.final-empty { background: #FFF; border: 1px solid #EAEAEA; border-top: none; border-radius: 0 0 10px 10px; padding: 26px 24px; color: #999; font-size: 13px; }

.toolbar { display: flex; align-items: center; gap: 14px; background: #FCFCFC; border: 1px solid #EAEAEA; border-top: none; padding: 10px 24px; }
.search { border: 1px solid #E5E5E5; border-radius: 6px; padding: 7px 10px; font-size: 12px; width: 260px; outline: none; font-family: inherit; }
.search:focus { border-color: #000; }
.tb-note { font-size: 11.5px; color: #888; flex: 1; }
.today-k { color: #0E7490; font-weight: 700; }
.tb-btn { border: 1px solid #000; background: #FFF; border-radius: 6px; padding: 6px 12px; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; cursor: pointer; }
.tb-btn:hover { background: #000; color: #FFF; }

.ftable { background: #FFF; border: 1px solid #EAEAEA; border-top: none; overflow-x: auto; max-height: 640px; overflow-y: auto; }
.f-row {
  display: grid; align-items: center; min-width: 1120px;
  grid-template-columns: 34px minmax(170px, 1.5fr) 132px minmax(130px, 1fr) 100px 150px 84px 84px 84px 150px 32px;
  border-bottom: 1px solid #F2F2F2; font-size: 12.5px;
}
.f-row.today { grid-template-columns: 34px minmax(170px, 1.5fr) 132px minmax(130px, 1fr) 100px 150px 84px 92px 84px 84px 150px 32px; min-width: 1210px; }
.f-row > span { padding: 9px 8px; min-width: 0; }
.f-row:hover:not(.f-head) { background: #FAFAFA; }
.f-head { position: sticky; top: 0; background: #FAFAFA; z-index: 2; border-bottom: 1px solid #EAEAEA; }
.f-head span { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 800; color: #999; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
.f-head .sortable { cursor: pointer; user-select: none; }
.f-head .on { color: #000; }
.f-head em { font-style: normal; margin-left: 3px; font-size: 8px; }
.r { text-align: right; }
.idx { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #BBB; text-align: right; }
.company { display: flex; flex-direction: column; }
.company b { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tick { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #FF5722; font-weight: 800; }
.sector { display: flex; align-items: center; gap: 7px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sector i { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.sector em { font-style: normal; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.industry { font-size: 12px; color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cap { font-variant-numeric: tabular-nums; font-weight: 600; display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.tier { font-family: 'JetBrains Mono', monospace; font-size: 8.5px; font-weight: 800; text-transform: uppercase; padding: 1px 5px; border-radius: 3px; background: #EEE; color: #666; }
.tier-mega { background: #000; color: #FFF; }
.tier-large { background: #FF5722; color: #FFF; }
.tier-mid { background: #FFCCBC; color: #8A2E0F; }
.strategy { display: flex; align-items: center; gap: 6px; }
.s-pill { color: #FFF; font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 10px; }
.s-pass { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; padding: 2px 6px; border-radius: 4px; background: #FBE9E9; color: #D13B3B; }
.s-pass.ok { background: #E3F5EC; color: #0F8A5F; }
.s-score { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #888; }
.none { font-size: 11px; color: #BBB; font-style: italic; }
.price { font-variant-numeric: tabular-nums; font-weight: 700; }
.today-v { color: #0E7490; display: flex; flex-direction: column; align-items: flex-end; }
.today-v i { font-style: normal; font-size: 10px; font-weight: 700; }
.today-v i.up { color: #0F8A5F; }
.today-v i.down { color: #D13B3B; }
.f-row > span.r:not(.price):not(.cap) { font-variant-numeric: tabular-nums; color: #444; }
.range { display: flex; flex-direction: column; gap: 4px; padding-left: 14px !important; padding-right: 14px !important; }
.rg-track { position: relative; height: 6px; background: linear-gradient(90deg, #FDE2E2, #F1F1F1 50%, #DDF3E8); border-radius: 3px; }
.rg-fill { position: absolute; left: 0; top: 0; bottom: 0; background: rgba(0,0,0,0.12); border-radius: 3px; }
.rg-dot { position: absolute; top: 50%; width: 10px; height: 10px; border-radius: 50%; background: #000; border: 2px solid #FFF; transform: translate(-50%, -50%); box-shadow: 0 0 0 1px #000; }
.rg-dot.today { background: #FFF; box-shadow: 0 0 0 1.5px #0E7490; width: 8px; height: 8px; }
.rg-off { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #888; }
.rm { text-align: center; }
.rm button { border: none; background: none; color: #CCC; cursor: pointer; font-size: 13px; padding: 2px 6px; border-radius: 4px; }
.rm button:hover { color: #D13B3B; background: #FBE9E9; }

.final-foot {
  display: flex; justify-content: space-between; align-items: center; gap: 16px; background: #000; color: #FFF;
  border-radius: 0 0 10px 10px; padding: 14px 24px; font-family: 'JetBrains Mono', monospace; font-size: 12px;
}
.lock-btn {
  background: #FF5722; color: #FFF; border: none; border-radius: 6px; padding: 12px 20px; font-family: 'JetBrains Mono', monospace;
  font-weight: 800; font-size: 12px; letter-spacing: 0.5px; cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
}
.lock-btn:disabled { background: #555; cursor: not-allowed; }
.spin { width: 11px; height: 11px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #FFF; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .final { padding: 0 16px; } .fh-stats { gap: 16px; } .toolbar { flex-wrap: wrap; } }
</style>
