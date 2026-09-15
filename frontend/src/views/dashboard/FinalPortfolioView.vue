<template>
  <div class="main-view">
    <div v-if="!portfolio" class="empty-shell">
      <p>No portfolio has been built yet this session.</p>
      <router-link to="/dashboard" class="btn-primary">Go to Dashboard →</router-link>
    </div>

    <template v-else>
      <!-- Header — same shell as MiroFish's original deep-interaction page (InteractionView.vue) -->
      <header class="app-header">
        <div class="header-left">
          <div class="brand" @click="router.push('/')">MIROFISH</div>
        </div>

        <div class="header-center">
          <div class="view-switcher">
            <button
              v-for="mode in ['portfolio', 'split', 'workbench']"
              :key="mode"
              class="switch-btn"
              :class="{ active: viewMode === mode }"
              @click="viewMode = mode"
            >
              {{ { portfolio: 'Portfolio', split: 'Split', workbench: 'Workbench' }[mode] }}
            </button>
          </div>
        </div>

        <div class="header-right">
          <router-link to="/dashboard" class="back-link">← Back to Dashboard</router-link>
          <div class="step-divider"></div>
          <div class="workflow-step">
            <span class="step-name">Final Portfolio</span>
          </div>
          <div class="step-divider"></div>
          <span class="status-indicator ready">
            <span class="dot"></span>
            Ready
          </span>
        </div>
      </header>

      <!-- Main Content Area -->
      <main class="content-area">
        <!-- Left Panel: the portfolio itself -->
        <div class="panel-wrapper left" :style="leftPanelStyle">
          <div class="results-scroll">
            <div class="summary-line">
              <strong>{{ knownTickers.join(', ') }}</strong>
              <span class="meta">
                as of {{ portfolio.as_of_date || 'live' }} · top_k={{ portfolio.top_k }} · max_weight={{ (portfolio.max_weight * 100).toFixed(0) }}%
              </span>
            </div>
            <PortfolioResultsPanel :portfolio="portfolio" @ticker-focus="onTickerFocus" />
          </div>
        </div>

        <!-- Right Panel: ticker picker + Chat / Debate Feed (Workbench) -->
        <div class="panel-wrapper right" :style="rightPanelStyle">
          <DashStep3Workbench
            :portfolio="portfolio"
            :activeTicker="activeTicker"
            @go-back="router.push('/dashboard')"
            @ticker-change="onTickerFocus"
            @add-log="addLog"
          />
        </div>
      </main>

      <!-- Bottom Terminal -->
      <div class="system-logs">
        <div class="log-header">
          <span class="log-title">SYSTEM LOG</span>
          <span class="log-id">{{ activeTicker || 'NO_TICKER' }}</span>
        </div>
        <div class="log-content" ref="logContent">
          <div class="log-line" v-for="(log, idx) in systemLogs" :key="idx">
            <span class="log-time">{{ log.time }}</span>
            <span class="log-msg">{{ log.msg }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getLastPortfolio } from '../../store/lastPortfolio'
import PortfolioResultsPanel from '../../components/dashboard/PortfolioResultsPanel.vue'
import DashStep3Workbench from '../../components/dashboard/DashStep3Workbench.vue'

const router = useRouter()
const portfolio = computed(() => getLastPortfolio())

const knownTickers = computed(() => {
  if (!portfolio.value) return []
  const out = new Set()
  Object.keys(portfolio.value.weights || {}).forEach(t => out.add(t))
  for (const h of portfolio.value.holdings || []) out.add(h.ticker)
  for (const e of portfolio.value.screening?.excluded_non_compliant || []) out.add(e.ticker)
  for (const s of portfolio.value.screening?.skipped || []) out.add(s.ticker)
  return Array.from(out).sort()
})

const activeTicker = ref('')
watch(knownTickers, (tickers) => {
  if (!activeTicker.value && tickers.length) activeTicker.value = tickers[0]
}, { immediate: true })

function onTickerFocus(ticker) {
  activeTicker.value = ticker
}

const viewMode = ref('split') // portfolio | split | workbench

const leftPanelStyle = computed(() => {
  if (viewMode.value === 'portfolio') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'workbench') return { width: '0%', opacity: 0, transform: 'translateX(-20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})
const rightPanelStyle = computed(() => {
  if (viewMode.value === 'workbench') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'portfolio') return { width: '0%', opacity: 0, transform: 'translateX(20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

// Terminal log
const systemLogs = ref([])
const logContent = ref(null)
function addLog(msg) {
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  systemLogs.value.push({ time, msg })
  if (systemLogs.value.length > 200) systemLogs.value.shift()
  nextTick(() => {
    if (logContent.value) logContent.value.scrollTop = logContent.value.scrollHeight
  })
}
</script>

<style scoped>
.main-view {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #FFF;
  overflow: hidden;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
}

.empty-shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.empty-shell p {
  color: #999;
}

.btn-primary {
  display: inline-block;
  background: #000;
  color: #FFF;
  padding: 10px 20px;
  font-size: 13px;
  border-radius: 3px;
  text-decoration: none;
}

/* Header (copied from InteractionView.vue for visual parity) */
.app-header {
  height: 60px;
  border-bottom: 1px solid #EAEAEA;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: #FFF;
  z-index: 100;
  position: relative;
  flex-shrink: 0;
}

.header-center {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.brand {
  font-weight: 800;
  font-size: 18px;
  letter-spacing: 1px;
  cursor: pointer;
}

.view-switcher {
  display: flex;
  background: #F5F5F5;
  padding: 4px;
  border-radius: 6px;
  gap: 4px;
}

.switch-btn {
  border: none;
  background: transparent;
  padding: 6px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #666;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
}

.switch-btn.active {
  background: #FFF;
  color: #000;
  box-shadow: 0 2px 4px rgba(0,0,0,0.05);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.back-link {
  font-size: 12px;
  color: #000;
  text-decoration: underline;
}

.workflow-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.step-name {
  font-weight: 700;
  color: #000;
}

.step-divider {
  width: 1px;
  height: 14px;
  background-color: #E0E0E0;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #666;
  font-weight: 500;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #CCC;
}

.status-indicator.ready .dot { background: #4CAF50; }

/* Content */
.content-area {
  flex: 1;
  display: flex;
  position: relative;
  overflow: hidden;
  min-height: 0;
}

.panel-wrapper {
  height: 100%;
  overflow: hidden;
  transition: width 0.4s cubic-bezier(0.25, 0.8, 0.25, 1), opacity 0.3s ease, transform 0.3s ease;
  will-change: width, opacity, transform;
  display: flex;
  flex-direction: column;
}

.panel-wrapper.left {
  border-right: 1px solid #EAEAEA;
}

.results-scroll {
  height: 100%;
  overflow-y: auto;
  padding: 18px;
}

.summary-line {
  margin-bottom: 16px;
  font-size: 13px;
}

.meta {
  color: #999;
  margin-left: 10px;
}

/* Terminal (adapted from Step3Simulation.vue's system-logs) */
.system-logs {
  flex-shrink: 0;
  height: 120px;
  border-top: 1px solid #EAEAEA;
  background: #0A0A0A;
  display: flex;
  flex-direction: column;
}

.log-header {
  display: flex;
  justify-content: space-between;
  padding: 6px 16px;
  border-bottom: 1px solid #222;
  font-size: 10px;
  color: #888;
  letter-spacing: 0.05em;
}

.log-content {
  flex: 1;
  overflow-y: auto;
  padding: 6px 16px;
}

.log-line {
  font-size: 11px;
  line-height: 1.6;
  display: flex;
  gap: 10px;
}

.log-time {
  color: #555;
  flex-shrink: 0;
}

.log-msg {
  color: #B0FFB0;
  word-break: break-word;
}
</style>
