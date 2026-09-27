<template>
  <div class="news-room">
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
      <div class="head-text">
        <div class="eyebrow"><span class="orange-sq"></span> STEP 02 / 06 · FINNHUB</div>
        <h1 class="title">News <span class="accent">Room</span></h1>
        <p class="sub" v-if="news">
          90 days of company news up to <b>{{ news.as_of_date }}</b>
          <span class="mono">({{ news.window_start }} → {{ news.as_of_date }})</span> —
          compacted into <code>txt_berita</code>, the reality seed for step 03.
        </p>
      </div>
      <div class="head-side">
        <PipelineStepRail v-if="run" :steps="run.steps" />
        <div class="controls">
          <span class="status-pill" :class="statusKey">
            <span class="dot"></span>{{ statusLabel }}
          </span>
          <button
            v-if="statusKey !== 'completed'"
            class="main-btn"
            :class="{ pause: isRunning }"
            :disabled="actionBusy || statusKey === 'pausing'"
            @click="isRunning ? pause() : start()"
          >
            <template v-if="isRunning">❚❚ PAUSE</template>
            <template v-else-if="statusKey === 'pausing'">PAUSING…</template>
            <template v-else-if="news?.fetched_count">▶ RESUME ({{ news.ticker_count - news.fetched_count }} left)</template>
            <template v-else>▶ FETCH NEWS</template>
          </button>
          <button v-else-if="!seedLocked" class="main-btn next" :disabled="actionBusy" @click="useAsSeed">
            {{ actionBusy ? 'FEEDING SEED…' : 'USE AS REALITY SEED →' }}
          </button>
          <button v-else class="main-btn next" @click="router.push(`/pipeline/${runId}`)">
            CONTINUE TO WORKSPACE →
          </button>
        </div>
      </div>
    </header>

    <div v-if="error" class="banner error">{{ error }}</div>
    <div v-if="news?.step?.error" class="banner error">{{ news.step.error }}</div>
    <div v-if="statusKey === 'interrupted'" class="banner warn">
      The backend restarted while fetching. Press <b>Resume</b> — tickers already saved are skipped.
    </div>

    <!-- Stats -->
    <section class="stats" v-if="news">
      <div class="stat">
        <div class="ring" :style="{ '--p': fetchPct }"><span>{{ fetchPct }}%</span></div>
        <div>
          <div class="stat-value">{{ news.fetched_count }}<small>/{{ news.ticker_count }}</small></div>
          <div class="stat-label">tickers fetched</div>
        </div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ fmtInt(news.totals.raw) }}</div>
        <div class="stat-label">raw articles</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ fmtInt(news.totals.relevant) }}</div>
        <div class="stat-label">about the company</div>
      </div>
      <div class="stat hi">
        <div class="stat-value">{{ fmtInt(news.totals.kept) }}</div>
        <div class="stat-label">kept in txt · cap {{ news.cap }}/ticker</div>
      </div>
      <div class="stat">
        <div class="stat-value">{{ txtKb }}<small> KB</small></div>
        <div class="gauge"><span :style="{ width: Math.min(100, txtKb / 3) + '%' }" :class="{ over: txtKb > 300 }"></span></div>
        <div class="stat-label">txt size · target ≤ 300 KB</div>
      </div>
    </section>

    <!-- Live tape -->
    <div class="tape" v-if="news?.live">
      <span class="tape-dot"></span>
      <span>Fetching <b>{{ news.live.current || '…' }}</b></span>
      <span v-if="news.live.last_window" class="mono">
        window {{ news.live.last_window.start }} → {{ news.live.last_window.end }} ·
        {{ news.live.last_window.count }} articles
        <em v-if="news.live.last_window.split">· capped, splitting</em>
      </span>
      <span class="mono tape-right">{{ news.live.calls }} API calls this session · ~60/min</span>
      <div class="tape-bar"><span :style="{ width: fetchPct + '%' }"></span></div>
    </div>

    <!-- Main -->
    <main class="grid" v-if="news">
      <!-- Tickers -->
      <aside class="col tickers">
        <div class="col-head">
          <span class="col-title">UNIVERSE</span>
          <input class="mini-search" v-model="tickerSearch" placeholder="Filter…" />
        </div>
        <div class="ticker-list">
          <div
            v-for="t in filteredTickers"
            :key="t.ticker"
            class="t-item"
            :class="[t.state, { sel: t.ticker === selected }]"
            @click="selectTicker(t.ticker)"
          >
            <span class="state-dot"></span>
            <div class="t-main">
              <div class="t-top">
                <span class="t-sym">{{ t.ticker }}</span>
                <span class="t-counts" v-if="t.state === 'fetched'"><b>{{ t.kept }}</b>/{{ fmtInt(t.raw) }}</span>
                <span class="t-counts" v-else-if="t.state === 'fetching'">fetching · {{ t.calls }} calls</span>
                <span class="t-counts err" v-else-if="t.state === 'error'">error</span>
                <span class="t-counts" v-else>queued</span>
              </div>
              <div class="t-name">{{ t.company_name }}</div>
              <svg v-if="t.state === 'fetched'" class="spark" viewBox="0 0 130 18" preserveAspectRatio="none">
                <g v-for="(v, i) in t.raw_by_week" :key="i">
                  <rect :x="i * 10" :y="18 - barH(v, t)" width="8" :height="barH(v, t)" class="raw" />
                  <rect :x="i * 10" :y="18 - Math.max(t.kept_by_week[i] ? 3 : 0, barH(t.kept_by_week[i], t, true))" width="8"
                        :height="Math.max(t.kept_by_week[i] ? 3 : 0, barH(t.kept_by_week[i], t, true))" class="kept" />
                </g>
              </svg>
              <div v-else-if="t.state === 'fetching'" class="fetch-line"></div>
            </div>
          </div>
        </div>
      </aside>

      <!-- Feed -->
      <section class="col feed">
        <template v-if="selectedInfo">
          <div class="feed-head">
            <div class="feed-title">
              <span class="feed-sym">{{ selectedInfo.ticker }}</span>
              <span class="feed-name">{{ selectedInfo.company_name }}</span>
              <span class="tier-badge" :class="`tier-${selectedInfo.market_cap_tier}`">{{ selectedInfo.market_cap_tier }}</span>
            </div>
            <div class="seg">
              <button v-for="v in feedViews" :key="v.key" :class="{ on: feedView === v.key }" @click="feedView = v.key">
                {{ v.label }} <span>{{ v.count }}</span>
              </button>
            </div>
          </div>

          <template v-if="selectedInfo.state === 'fetched'">
            <!-- week histogram -->
            <div class="weeks">
              <div
                v-for="(v, i) in selectedInfo.raw_by_week"
                :key="i"
                class="week"
                :class="{ on: weekFilter === i }"
                @click="weekFilter = weekFilter === i ? null : i"
                :title="`${weekLabel(i)} · ${v} raw · ${selectedInfo.kept_by_week[i]} kept`"
              >
                <div class="week-bar">
                  <span class="raw" :style="{ height: weekH(v) + '%' }"></span>
                  <span class="kept" :style="{ height: weekH(selectedInfo.kept_by_week[i], true) + '%' }"></span>
                </div>
                <span class="week-label">{{ weekLabel(i) }}</span>
              </div>
            </div>

            <div class="tag-row">
              <button
                v-for="tag in tagList"
                :key="tag"
                class="tag-chip"
                :class="[`tag-${tag.replace('&', '')}`, { on: tagFilter === tag }]"
                @click="tagFilter = tagFilter === tag ? null : tag"
              >{{ tag }} <span>{{ tagCounts[tag] || 0 }}</span></button>
              <input class="mini-search wide" v-model="articleSearch" placeholder="Search headlines…" />
            </div>

            <div class="cards" ref="cardsRef">
              <div v-if="articlesLoading" class="feed-empty">Loading articles…</div>
              <article
                v-for="a in shownArticles"
                :key="a.article_id"
                class="card"
                :class="a.decision === 'kept' ? 'kept' : 'dropped'"
              >
                <div class="card-date">
                  <span class="d-day">{{ a.published_at.slice(8, 10) }}</span>
                  <span class="d-mon">{{ monthName(a.published_at) }}</span>
                </div>
                <div class="card-body">
                  <div class="card-meta">
                    <span class="pub">{{ a.publisher || 'Unknown' }}</span>
                    <span v-for="tag in a.tags" :key="tag" class="mini-tag" :class="`tag-${tag.replace('&', '')}`">{{ tag }}</span>
                    <span class="score">score {{ a.score }}</span>
                    <span v-if="a.decision === 'kept'" class="verdict kept">IN TXT</span>
                    <span v-else class="verdict">{{ news.drop_labels[a.decision] }}</span>
                  </div>
                  <a class="headline" :href="a.url" target="_blank" rel="noopener">{{ a.headline }}</a>
                  <p class="summary" v-if="a.clean_summary">{{ a.clean_summary }}</p>
                </div>
              </article>
              <button v-if="filteredArticles.length > shownArticles.length" class="more-btn" @click="pageSize += 100">
                Show {{ Math.min(100, filteredArticles.length - shownArticles.length) }} more
                ({{ filteredArticles.length - shownArticles.length }} hidden)
              </button>
              <div v-if="!articlesLoading && !filteredArticles.length" class="feed-empty">No articles match.</div>
            </div>
          </template>
          <div v-else class="feed-empty big">
            <div class="empty-icon">◌</div>
            <p v-if="selectedInfo.state === 'fetching'">Fetching {{ selectedInfo.ticker }} right now…</p>
            <p v-else-if="selectedInfo.state === 'error'">{{ selectedInfo.error }}</p>
            <p v-else>{{ selectedInfo.ticker }} is queued. Press <b>Fetch news</b> to start.</p>
          </div>
        </template>
      </section>

      <!-- txt_berita -->
      <section class="col txt">
        <div class="txt-head">
          <div>
            <div class="txt-title">txt_berita.txt</div>
            <div class="txt-meta" v-if="txtData">{{ fmtInt(news.txt?.totals?.lines) }} articles · {{ txtKb }} KB · built {{ news.txt?.generated_at?.replace('T', ' ') }}</div>
            <div class="txt-meta" v-else>Built automatically once every ticker is fetched</div>
          </div>
          <a v-if="txtData" class="icon-btn" :href="downloadUrl" title="Download">⤓</a>
        </div>

        <div class="cap-box">
          <div class="cap-top">
            <span>Articles per ticker</span>
            <b>{{ capDraft }}</b>
          </div>
          <input type="range" min="5" max="50" v-model.number="capDraft" class="slider" :disabled="seedLocked" />
          <div class="cap-foot">
            <span class="mono">≈ {{ fmtInt(estLines) }} articles · ~{{ estKb }} KB</span>
            <button class="link" @click="capDraft = news.auto_cap">auto ({{ news.auto_cap }})</button>
          </div>
          <button
            class="rebuild-btn"
            :disabled="!canRebuild || rebuilding"
            @click="rebuild"
          >{{ seedLocked ? `Locked — reality seed of ${news.seed.project_id}` : rebuilding ? 'Rebuilding…' : capDraft === news.cap && txtData ? 'txt is up to date' : 'Rebuild txt with this cap' }}</button>
        </div>

        <div class="txt-view" ref="txtRef">
          <template v-if="txtData">
            <div
              v-for="(line, i) in txtLines"
              :key="i"
              class="tl"
              :class="line.kind"
              :id="line.kind === 'section' ? `sec-${line.ticker}` : undefined"
            >
              <template v-if="line.kind === 'item'">
                <span class="tl-date">{{ line.date }}</span>
                <span class="tl-tk">{{ line.tickers }}</span>
                <span class="tl-hl">{{ line.headline }}</span>
                <span v-if="line.summary" class="tl-sum"> — {{ line.summary }}</span>
              </template>
              <template v-else>{{ line.text || ' ' }}</template>
            </div>
          </template>
          <div v-else class="txt-empty">
            <div class="txt-skeleton" v-for="n in 14" :key="n" :style="{ width: 40 + (n * 37) % 55 + '%' }"></div>
          </div>
        </div>
      </section>
    </main>

    <!-- run log -->
    <footer class="runlog" v-if="logs.length">
      <span class="runlog-title">RUN.LOG</span>
      <div class="runlog-lines">
        <span v-for="(l, i) in logs.slice(-3)" :key="i"><em>{{ l.time.slice(11) }}</em> {{ l.message }}</span>
      </div>
    </footer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import PipelineStepRail from '../../components/pipeline/PipelineStepRail.vue'
import {
  getRun, getRunLog, getNewsStatus, startNews, pauseNews,
  getNewsArticles, compactNews, getNewsTxt, newsTxtDownloadUrl, feedSeed
} from '../../api/pipeline'

const props = defineProps({ runId: { type: String, required: true } })
const router = useRouter()

const run = ref(null)
const news = ref(null)
const logs = ref([])
const error = ref('')
const actionBusy = ref(false)

const selected = ref(null)
const tickerSearch = ref('')
const articles = ref([])
const articlesLoading = ref(false)
const feedView = ref('kept')
const weekFilter = ref(null)
const tagFilter = ref(null)
const articleSearch = ref('')
const pageSize = ref(100)
const cardsRef = ref(null)

const txtData = ref(null)
const txtRef = ref(null)
const capDraft = ref(20)
const rebuilding = ref(false)
const downloadUrl = computed(() => newsTxtDownloadUrl(props.runId))

let timer = null
let lastTxtStamp = null

const TAGS = ['EARNINGS', 'M&A', 'REGULATORY', 'LEGAL', 'ANALYST', 'CAPITAL', 'LEADERSHIP', 'STRATEGY', 'MACRO']
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

const fmtInt = (n) => (n ?? 0).toLocaleString('en-US')
const monthName = (iso) => MONTHS[Number(iso.slice(5, 7)) - 1]

// ---------------- status ----------------
const statusKey = computed(() => news.value?.status || 'pending')
const isRunning = computed(() => statusKey.value === 'running')
const statusLabel = computed(() => ({
  pending: 'Not started', running: 'Fetching', pausing: 'Pausing', paused: 'Paused',
  interrupted: 'Interrupted', failed: 'Needs retry', completed: 'Complete'
}[statusKey.value] || statusKey.value))
const fetchPct = computed(() => news.value ? Math.round(news.value.fetched_count * 100 / Math.max(1, news.value.ticker_count)) : 0)
const txtKb = computed(() => txtData.value ? Math.round(txtData.value.bytes / 1024) : 0)

// ---------------- tickers ----------------
const filteredTickers = computed(() => {
  const q = tickerSearch.value.trim().toLowerCase()
  return (news.value?.tickers || []).filter(t => !q || t.ticker.toLowerCase().includes(q) || t.company_name.toLowerCase().includes(q))
})
const selectedInfo = computed(() => news.value?.tickers.find(t => t.ticker === selected.value) || null)
const maxWeek = computed(() => Math.max(1, ...(selectedInfo.value?.raw_by_week || [1])))
const maxKeptWeek = computed(() => Math.max(1, ...(selectedInfo.value?.kept_by_week || [1])))
const barH = (v, t, kept = false) => {
  const max = Math.max(1, ...(kept ? t.kept_by_week : t.raw_by_week))
  return (v / max) * (kept ? 10 : 18)
}
const weekH = (v, kept = false) => Math.max(v ? 4 : 0, (v / (kept ? maxKeptWeek.value * 2.2 : maxWeek.value)) * 100)
const weekLabel = (i) => {
  if (!news.value) return ''
  const d = new Date(news.value.window_start)
  d.setDate(d.getDate() + i * 7)
  return `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}`
}

const selectTicker = (ticker) => {
  selected.value = ticker
  weekFilter.value = null
  tagFilter.value = null
  articleSearch.value = ''
  scrollTxtTo(ticker)
}

// ---------------- articles ----------------
const loadArticles = async () => {
  if (!selectedInfo.value || selectedInfo.value.state !== 'fetched') {
    articles.value = []
    return
  }
  articlesLoading.value = true
  try {
    const res = await getNewsArticles(props.runId, selected.value, 'all')
    articles.value = res.data
  } catch (e) {
    error.value = e.message
  } finally {
    articlesLoading.value = false
  }
}

const feedViews = computed(() => {
  const kept = articles.value.filter(a => a.decision === 'kept').length
  return [
    { key: 'kept', label: 'In txt', count: kept },
    { key: 'dropped', label: 'Dropped', count: articles.value.length - kept },
    { key: 'all', label: 'All fetched', count: articles.value.length }
  ]
})
const viewArticles = computed(() => articles.value.filter(a =>
  feedView.value === 'all' || (feedView.value === 'kept') === (a.decision === 'kept')))
const tagCounts = computed(() => {
  const c = {}
  viewArticles.value.forEach(a => a.tags.forEach(t => { c[t] = (c[t] || 0) + 1 }))
  return c
})
const tagList = computed(() => TAGS.filter(t => tagCounts.value[t]))
const filteredArticles = computed(() => {
  const q = articleSearch.value.trim().toLowerCase()
  return viewArticles.value.filter(a =>
    (weekFilter.value === null || Math.min(a.week, 12) === weekFilter.value) &&
    (!tagFilter.value || a.tags.includes(tagFilter.value)) &&
    (!q || a.headline.toLowerCase().includes(q) || (a.clean_summary || '').toLowerCase().includes(q)))
})
const shownArticles = computed(() => filteredArticles.value.slice(0, pageSize.value))

watch([feedView, weekFilter, tagFilter, articleSearch], () => {
  pageSize.value = 100
  if (cardsRef.value) cardsRef.value.scrollTop = 0
})

// ---------------- txt ----------------
const txtLines = computed(() => {
  if (!txtData.value) return []
  return txtData.value.text.split('\n').map(line => {
    const sec = line.match(/^=== (\S+) — /)
    if (sec) return { kind: 'section', text: line, ticker: sec[1] }
    const item = line.match(/^(\d{4}-\d{2}-\d{2}) \| ([^|]+) \| (.*)$/)
    if (item) {
      const [headline, ...rest] = item[3].split(' — ')
      return { kind: 'item', date: item[1], tickers: item[2], headline, summary: rest.join(' — ') }
    }
    return { kind: line.startsWith('(no relevant') ? 'none' : 'plain', text: line }
  })
})

const bytesPerArticle = computed(() => {
  const t = news.value?.txt?.totals
  return t && t.lines ? t.bytes / t.lines : 330
})
const estLines = computed(() => {
  if (!news.value) return 0
  // kept can't exceed each ticker's usable (relevant, non-noise, non-dup) count
  return news.value.tickers.reduce((s, t) => {
    if (t.state !== 'fetched') return s + capDraft.value
    const usable = t.kept + (t.dropped?.over_cap || 0)
    return s + Math.min(capDraft.value, usable)
  }, 0)
})
const estKb = computed(() => Math.round(estLines.value * bytesPerArticle.value / 1024))
const seedLocked = computed(() => news.value?.seed?.status === 'completed')
const canRebuild = computed(() => news.value && !seedLocked.value && news.value.fetched_count === news.value.ticker_count &&
  !isRunning.value && (capDraft.value !== news.value.cap || !txtData.value))

const loadTxt = async () => {
  try {
    const res = await getNewsTxt(props.runId)
    txtData.value = res.data
  } catch {
    txtData.value = null
  }
}

const scrollTxtTo = async (ticker) => {
  await nextTick()
  const el = txtRef.value?.querySelector(`#sec-${CSS.escape(ticker)}`)
  if (el && txtRef.value) txtRef.value.scrollTo({ top: el.offsetTop - txtRef.value.offsetTop - 8, behavior: 'smooth' })
}

const rebuild = async () => {
  rebuilding.value = true
  try {
    await compactNews(props.runId, capDraft.value)
    await refresh()
    await loadArticles()
    if (selected.value) scrollTxtTo(selected.value)
  } catch (e) {
    error.value = e.message
  } finally {
    rebuilding.value = false
  }
}

// ---------------- actions / polling ----------------
const start = async () => {
  actionBusy.value = true
  error.value = ''
  try {
    news.value = (await startNews(props.runId)).data
    schedule()
  } catch (e) {
    error.value = e.message
  } finally {
    actionBusy.value = false
  }
}

// step 3 (no UI of its own): lock txt_berita in as the MiroFish reality seed
const useAsSeed = async () => {
  actionBusy.value = true
  error.value = ''
  try {
    await feedSeed(props.runId)
    router.push(`/pipeline/${props.runId}`)
  } catch (e) {
    error.value = e.message
  } finally {
    actionBusy.value = false
  }
}

const pause = async () => {
  actionBusy.value = true
  try {
    news.value = (await pauseNews(props.runId)).data
  } catch (e) {
    error.value = e.message
  } finally {
    actionBusy.value = false
  }
}

const refresh = async () => {
  const prevState = selectedInfo.value?.state
  const [n, r, l] = await Promise.all([getNewsStatus(props.runId), getRun(props.runId), getRunLog(props.runId, 20)])
  const firstLoad = !news.value
  news.value = n.data
  run.value = r.data
  logs.value = l.data
  if (firstLoad) capDraft.value = n.data.cap
  if (!selected.value) {
    const first = n.data.tickers.find(t => t.state === 'fetched') || n.data.tickers[0]
    selected.value = first?.ticker
  }
  if (selectedInfo.value?.state === 'fetched' && (prevState !== 'fetched' || firstLoad)) loadArticles()
  const stamp = n.data.txt?.generated_at || null
  if (stamp !== lastTxtStamp) {
    lastTxtStamp = stamp
    if (stamp) {
      await loadTxt()
      if (!rebuilding.value) capDraft.value = n.data.cap
    }
  }
}

const schedule = () => {
  clearTimeout(timer)
  const active = ['running', 'pausing'].includes(news.value?.status)
  timer = setTimeout(async () => {
    try {
      await refresh()
    } catch (e) {
      error.value = e.message
    }
    schedule()
  }, active ? 1500 : 6000)
}

watch(selected, (t, old) => { if (t && t !== old) loadArticles() })

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    error.value = e.message
  }
  schedule()
})
onUnmounted(() => clearTimeout(timer))
</script>

<style scoped>
.news-room {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #FAFAFA;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
  color: #000;
  overflow: hidden;
  background-image:
    linear-gradient(rgba(0,0,0,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,0,0,0.03) 1px, transparent 1px);
  background-size: 32px 32px;
}
code, .mono { font-family: 'JetBrains Mono', monospace; }
code { font-size: 0.9em; background: #F0F0F0; padding: 1px 5px; border-radius: 3px; }

/* navbar */
.navbar {
  height: 52px;
  flex-shrink: 0;
  background: #000;
  color: #FFF;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 28px;
}
.nav-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.nav-brand { font-family: 'JetBrains Mono', monospace; font-weight: 800; letter-spacing: 1px; font-size: 1.1rem; cursor: pointer; }
.nav-tag { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; background: #FF5722; padding: 3px 8px; letter-spacing: 1px; }
.crumb { font-size: 12px; color: #999; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.crumb b { color: #FFF; font-weight: 600; }
.nav-right { display: flex; gap: 20px; }
.nav-link { font-family: 'JetBrains Mono', monospace; font-size: 12px; opacity: 0.75; cursor: pointer; }
.nav-link:hover { opacity: 1; }

/* header */
.head {
  flex-shrink: 0;
  display: grid;
  grid-template-columns: 1fr 560px;
  gap: 32px;
  align-items: end;
  padding: 18px 28px 14px;
}
.eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 700; color: #999; letter-spacing: 1.5px; display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.orange-sq { width: 8px; height: 8px; background: #FF5722; display: inline-block; }
.title { font-size: 2.1rem; font-weight: 600; letter-spacing: -1.2px; line-height: 1.05; margin-bottom: 6px; }
.accent { background: linear-gradient(90deg, #000 0%, #FF5722 100%); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.sub { font-size: 13px; color: #555; }
.sub .mono { font-size: 11.5px; color: #999; }
.head-side { display: flex; flex-direction: column; gap: 12px; }
.controls { display: flex; justify-content: flex-end; align-items: center; gap: 12px; }
.status-pill {
  display: flex; align-items: center; gap: 7px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700;
  padding: 7px 11px; border-radius: 20px; background: #F0F0F0; color: #666;
}
.status-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: #BBB; }
.status-pill.running .dot, .status-pill.pausing .dot { background: #FF5722; animation: pulse 0.9s infinite; }
.status-pill.running { background: #FFF1EC; color: #FF5722; }
.status-pill.completed { background: #E8F5E9; color: #2E7D32; }
.status-pill.completed .dot { background: #4CAF50; }
.status-pill.failed, .status-pill.interrupted { background: #FFEBEE; color: #C62828; }
.status-pill.failed .dot, .status-pill.interrupted .dot { background: #F44336; }
.main-btn {
  background: #000; color: #FFF; border: none; border-radius: 6px;
  padding: 12px 20px; font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 12px; letter-spacing: 0.8px;
  cursor: pointer; transition: all 0.2s;
}
.main-btn:hover:not(:disabled) { background: #FF5722; box-shadow: 0 6px 18px rgba(255,87,34,0.3); transform: translateY(-1px); }
.main-btn.pause { background: #FFF; color: #000; box-shadow: inset 0 0 0 1.5px #000; }
.main-btn.pause:hover { background: #000; color: #FFF; }
.main-btn.next { background: #FF5722; }
.main-btn:disabled { background: #CCC; cursor: not-allowed; }

.banner { margin: 0 28px 10px; padding: 9px 12px; border-radius: 6px; font-size: 12px; flex-shrink: 0; }
.banner.error { background: #FFEBEE; color: #C62828; border-left: 3px solid #F44336; }
.banner.warn { background: #FFF8E1; color: #8D6E00; border-left: 3px solid #FFB300; }

/* stats */
.stats {
  flex-shrink: 0;
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr 1fr 1.2fr;
  gap: 10px;
  padding: 0 28px 12px;
}
.stat {
  background: #FFF; border: 1px solid #EAEAEA; border-radius: 8px; padding: 12px 14px;
  display: flex; flex-direction: column; justify-content: center; gap: 2px;
}
.stat:first-child { flex-direction: row; align-items: center; justify-content: flex-start; gap: 14px; }
.stat.hi { background: #000; border-color: #000; color: #FFF; }
.stat.hi .stat-label { color: #FF8A65; }
.stat-value { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 22px; letter-spacing: -0.5px; }
.stat-value small { font-size: 13px; color: #AAA; font-weight: 600; }
.stat-label { font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: 0.6px; }
.ring {
  --p: 0;
  width: 44px; height: 44px; border-radius: 50%; flex-shrink: 0;
  background: conic-gradient(#FF5722 calc(var(--p) * 1%), #F0F0F0 0);
  display: grid; place-items: center;
}
.ring span {
  width: 34px; height: 34px; border-radius: 50%; background: #FFF;
  display: grid; place-items: center; font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700;
}
.gauge { height: 4px; background: #F0F0F0; border-radius: 2px; overflow: hidden; margin: 5px 0 3px; }
.gauge span { display: block; height: 100%; background: #000; transition: width 0.4s; }
.gauge span.over { background: #F44336; }

/* live tape */
.tape {
  flex-shrink: 0;
  margin: 0 28px 12px;
  position: relative;
  display: flex; align-items: center; gap: 14px;
  background: #000; color: #EEE; border-radius: 6px; padding: 9px 14px 12px;
  font-size: 12px; overflow: hidden;
}
.tape b { color: #FF8A65; font-family: 'JetBrains Mono', monospace; }
.tape .mono { font-size: 11px; color: #AAA; }
.tape em { color: #FFB74D; font-style: normal; }
.tape-right { margin-left: auto; }
.tape-dot { width: 8px; height: 8px; border-radius: 50%; background: #FF5722; animation: pulse 0.8s infinite; }
.tape-bar { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: #222; }
.tape-bar span { display: block; height: 100%; background: linear-gradient(90deg, #FF5722, #FFAB91); transition: width 0.6s; }

/* grid */
.grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr) 440px;
  gap: 12px;
  padding: 0 28px 12px;
}
.col {
  background: #FFF; border: 1px solid #EAEAEA; border-radius: 8px;
  display: flex; flex-direction: column; min-height: 0; overflow: hidden;
}
.col-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 12px 8px; border-bottom: 1px solid #F2F2F2; }
.col-title { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; color: #999; letter-spacing: 1px; }
.mini-search {
  border: 1px solid #E5E5E5; border-radius: 5px; padding: 5px 8px; font-size: 11.5px; width: 110px; outline: none; font-family: inherit;
}
.mini-search:focus { border-color: #000; }
.mini-search.wide { width: 180px; margin-left: auto; }

/* ticker list */
.ticker-list { flex: 1; overflow-y: auto; padding: 6px; }
.t-item {
  display: flex; gap: 10px; padding: 8px 8px; border-radius: 6px; cursor: pointer;
  border: 1px solid transparent; transition: background 0.15s;
}
.t-item:hover { background: #F7F7F7; }
.t-item.sel { background: #FFF4F0; border-color: #FFCCBC; }
.state-dot { width: 7px; height: 7px; border-radius: 50%; background: #DDD; margin-top: 6px; flex-shrink: 0; }
.t-item.fetched .state-dot { background: #000; }
.t-item.fetching .state-dot { background: #FF5722; animation: pulse 0.7s infinite; box-shadow: 0 0 0 3px rgba(255,87,34,0.2); }
.t-item.error .state-dot { background: #F44336; }
.t-main { flex: 1; min-width: 0; }
.t-top { display: flex; justify-content: space-between; align-items: baseline; }
.t-sym { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 13px; }
.t-item.pending .t-sym { color: #AAA; }
.t-counts { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #999; }
.t-counts b { color: #FF5722; }
.t-counts.err { color: #F44336; }
.t-name { font-size: 11px; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 1px 0 4px; }
.spark { width: 100%; height: 18px; display: block; }
.spark .raw { fill: #E6E6E6; }
.spark .kept { fill: #FF5722; }
.t-item.sel .spark .raw { fill: #FFD5C7; }
.fetch-line { height: 3px; border-radius: 2px; background: linear-gradient(90deg, transparent, #FF5722, transparent); background-size: 200% 100%; animation: slide 1s linear infinite; margin-top: 7px; }

/* feed */
.feed-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px 10px; border-bottom: 1px solid #F2F2F2; flex-wrap: wrap; }
.feed-title { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.feed-sym { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 20px; }
.feed-name { font-size: 13px; color: #666; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tier-badge { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 800; padding: 3px 7px; border-radius: 3px; text-transform: uppercase; color: #FFF; align-self: center; }
.tier-badge.tier-mega { background: #000; }
.tier-badge.tier-large { background: #FF5722; }
.tier-badge.tier-mid { background: #FFAB91; color: #5D2A1A; }
.tier-badge.tier-small { background: #E0E0E0; color: #555; }
.seg { display: flex; background: #F4F4F4; padding: 3px; border-radius: 6px; gap: 2px; }
.seg button { border: none; background: none; padding: 6px 10px; border-radius: 4px; font-size: 11.5px; font-weight: 600; color: #777; cursor: pointer; }
.seg button span { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #FF5722; margin-left: 3px; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

.weeks { display: grid; grid-template-columns: repeat(13, 1fr); gap: 4px; padding: 12px 16px 6px; }
.week { cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.week-bar { position: relative; width: 100%; height: 46px; background: #FAFAFA; border-radius: 3px; overflow: hidden; }
.week-bar span { position: absolute; left: 0; right: 0; bottom: 0; border-radius: 3px 3px 0 0; transition: height 0.4s; }
.week-bar .raw { background: #E8E8E8; }
.week-bar .kept { background: #FF5722; left: 30%; right: 30%; }
.week:hover .week-bar .raw { background: #D8D8D8; }
.week.on .week-bar { box-shadow: inset 0 0 0 1.5px #000; }
.week-label { font-family: 'JetBrains Mono', monospace; font-size: 8.5px; color: #AAA; white-space: nowrap; }
.week.on .week-label { color: #000; font-weight: 700; }

.tag-row { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 6px 16px 10px; border-bottom: 1px solid #F2F2F2; }
.tag-chip {
  border: 1px solid #E8E8E8; background: #FFF; border-radius: 12px; padding: 3px 9px;
  font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; cursor: pointer; color: var(--c, #555);
}
.tag-chip span { color: #AAA; font-weight: 600; margin-left: 2px; }
.tag-chip.on { background: var(--c, #000); color: #FFF; border-color: var(--c, #000); }
.tag-chip.on span { color: rgba(255,255,255,0.75); }

.cards { flex: 1; overflow-y: auto; padding: 10px 16px 16px; display: flex; flex-direction: column; gap: 8px; }
.card {
  display: flex; gap: 14px; padding: 12px 14px; border-radius: 7px; border: 1px solid #EFEFEF; background: #FFF;
  position: relative; transition: border-color 0.15s, box-shadow 0.15s;
}
.card.kept { border-left: 3px solid #FF5722; }
.card.kept:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.05); border-color: #FFCCBC; }
.card.dropped { opacity: 0.55; background: #FCFCFC; }
.card.dropped:hover { opacity: 0.9; }
.card-date { display: flex; flex-direction: column; align-items: center; width: 34px; flex-shrink: 0; padding-top: 2px; }
.d-day { font-family: 'JetBrains Mono', monospace; font-size: 18px; font-weight: 800; line-height: 1; }
.d-mon { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #999; font-weight: 700; margin-top: 3px; }
.card-body { flex: 1; min-width: 0; }
.card-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 5px; }
.pub { font-size: 10.5px; font-weight: 700; color: #666; }
.mini-tag { font-family: 'JetBrains Mono', monospace; font-size: 8.5px; font-weight: 800; padding: 2px 5px; border-radius: 3px; color: #FFF; background: var(--c, #555); }
.score { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #BBB; }
.verdict { margin-left: auto; font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 700; color: #999; background: #F2F2F2; padding: 2px 6px; border-radius: 3px; }
.verdict.kept { background: #FF5722; color: #FFF; }
.headline { display: block; font-size: 13.5px; font-weight: 600; color: #111; text-decoration: none; line-height: 1.35; }
.headline:hover { text-decoration: underline; }
.summary { font-size: 12px; color: #666; line-height: 1.5; margin-top: 4px; }
.more-btn { border: 1px dashed #DDD; background: #FFF; border-radius: 6px; padding: 10px; font-size: 12px; color: #666; cursor: pointer; }
.more-btn:hover { border-color: #000; color: #000; }
.feed-empty { padding: 30px; text-align: center; color: #AAA; font-size: 12.5px; }
.feed-empty.big { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; }
.empty-icon { font-size: 34px; color: #DDD; animation: spin 3s linear infinite; }

/* tag palette */
.tag-EARNINGS { --c: #FF5722; }
.tag-MA { --c: #7C4DFF; }
.tag-REGULATORY { --c: #00897B; }
.tag-LEGAL { --c: #E53935; }
.tag-ANALYST { --c: #1E88E5; }
.tag-CAPITAL { --c: #43A047; }
.tag-LEADERSHIP { --c: #6D4C41; }
.tag-STRATEGY { --c: #F9A825; }
.tag-MACRO { --c: #546E7A; }

/* txt panel */
.col.txt { background: #0B0B0B; border-color: #0B0B0B; color: #DDD; }
.txt-head { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px 10px; border-bottom: 1px solid #222; }
.txt-title { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 14px; color: #FFF; }
.txt-meta { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #777; margin-top: 3px; }
.icon-btn {
  width: 32px; height: 32px; border-radius: 6px; background: #FF5722; color: #FFF; display: grid; place-items: center;
  text-decoration: none; font-size: 16px; font-weight: 800;
}
.icon-btn:hover { background: #FF7043; }
.cap-box { padding: 12px 16px; border-bottom: 1px solid #222; display: flex; flex-direction: column; gap: 6px; }
.cap-top { display: flex; justify-content: space-between; font-size: 11.5px; color: #AAA; }
.cap-top b { font-family: 'JetBrains Mono', monospace; color: #FF8A65; font-size: 14px; }
.slider { width: 100%; accent-color: #FF5722; }
.cap-foot { display: flex; justify-content: space-between; align-items: center; font-size: 10.5px; color: #777; }
.link { background: none; border: none; color: #FF8A65; font-family: 'JetBrains Mono', monospace; font-size: 10.5px; cursor: pointer; }
.rebuild-btn {
  margin-top: 2px; background: #1C1C1C; color: #FFF; border: 1px solid #333; border-radius: 5px; padding: 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; cursor: pointer;
}
.rebuild-btn:hover:not(:disabled) { background: #FF5722; border-color: #FF5722; }
.rebuild-btn:disabled { color: #666; cursor: default; }
.txt-view { flex: 1; overflow-y: auto; padding: 12px 16px 20px; font-family: 'JetBrains Mono', monospace; font-size: 10.5px; line-height: 1.6; }
.tl { white-space: pre-wrap; word-break: break-word; }
.tl.plain { color: #888; }
.tl.section { color: #FF8A65; font-weight: 800; margin-top: 10px; padding: 4px 0; border-top: 1px dashed #2A2A2A; }
.tl.none { color: #666; font-style: italic; }
.tl.item { color: #BBB; padding: 1px 0; }
.tl-date { color: #666; margin-right: 6px; }
.tl-tk { color: #FF8A65; margin-right: 6px; }
.tl-hl { color: #F2F2F2; }
.tl-sum { color: #888; }
.txt-empty { display: flex; flex-direction: column; gap: 9px; padding-top: 6px; }
.txt-skeleton { height: 9px; border-radius: 3px; background: linear-gradient(90deg, #1A1A1A, #262626, #1A1A1A); background-size: 200% 100%; animation: slide 1.4s linear infinite; }

/* run log */
.runlog {
  flex-shrink: 0; display: flex; align-items: center; gap: 14px; background: #000; color: #BBB; padding: 7px 28px;
  font-family: 'JetBrains Mono', monospace; font-size: 10.5px; overflow: hidden;
}
.runlog-title { color: #FF5722; font-weight: 800; flex-shrink: 0; }
.runlog-lines { display: flex; gap: 22px; overflow: hidden; white-space: nowrap; }
.runlog-lines em { color: #666; font-style: normal; }

@keyframes pulse { 50% { opacity: 0.35; } }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes slide { from { background-position: 200% 0; } to { background-position: -200% 0; } }

@media (max-width: 1250px) {
  .news-room { height: auto; overflow: visible; }
  .head { grid-template-columns: 1fr; }
  .grid { grid-template-columns: 1fr; }
  .col { max-height: 80vh; }
  .stats { grid-template-columns: repeat(2, 1fr); }
}
</style>
