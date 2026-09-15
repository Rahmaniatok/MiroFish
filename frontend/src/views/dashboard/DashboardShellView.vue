<template>
  <div class="main-view">
    <!-- Header (mirrors MainView.vue's OASIS shell) -->
    <header class="app-header">
      <div class="header-left">
        <div class="brand" @click="router.push('/')">MIROFISH</div>
      </div>

      <div class="header-center">
        <div class="view-switcher">
          <button
            v-for="mode in ['graph', 'split', 'workbench']"
            :key="mode"
            class="switch-btn"
            :class="{ active: viewMode === mode }"
            @click="viewMode = mode"
          >
            {{ { graph: 'Graph', split: 'Split', workbench: 'Workbench' }[mode] }}
          </button>
        </div>
      </div>

      <div class="header-right">
        <router-link v-if="portfolio" to="/dashboard/portfolio" class="final-portfolio-link">
          Final Portfolio →
        </router-link>
        <div class="step-divider"></div>
        <div class="workflow-step">
          <span class="step-num">Step {{ currentStep }}/3</span>
          <span class="step-name">{{ stepNames[currentStep - 1] }}</span>
        </div>
        <div class="step-divider"></div>
        <span class="status-indicator" :class="statusClass">
          <span class="dot"></span>
          {{ statusText }}
        </span>
      </div>
    </header>

    <!-- Main Content Area -->
    <main class="content-area">
      <!-- Left Panel: Graph -->
      <div class="panel-wrapper left" :style="leftPanelStyle">
        <GraphPanel
          :graphData="graphData"
          :loading="graphLoading"
          :currentPhase="2"
          :isSimulating="false"
          :emptyStateText="graphEmptyText"
          @refresh="() => activeTicker && loadTickerGraph(activeTicker)"
          @toggle-maximize="toggleMaximize('graph')"
        />
      </div>

      <!-- Right Panel: Step Components -->
      <div class="panel-wrapper right" :style="rightPanelStyle">
        <DashStep1Screening
          v-if="currentStep === 1"
          @ticker-focus="onTickerFocus"
          @add-log="addLog"
          @continue="handleStep1Continue"
        />
        <DashStep2PortfolioBuild
          v-else-if="currentStep === 2"
          :candidateTickers="candidateTickers"
          @go-back="handleGoBack"
          @ticker-focus="onTickerFocus"
          @add-log="addLog"
          @built="handleStep2Built"
        />
        <DashStep3Workbench
          v-else-if="currentStep === 3"
          :portfolio="portfolio"
          :activeTicker="activeTicker"
          @go-back="handleGoBack"
          @ticker-change="onTickerFocus"
          @add-log="addLog"
        />
      </div>
    </main>

    <!-- Bottom Terminal (shared across all 3 steps, unlike the old per-step duplication) -->
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
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import GraphPanel from '../../components/GraphPanel.vue'
import DashStep1Screening from '../../components/dashboard/DashStep1Screening.vue'
import DashStep2PortfolioBuild from '../../components/dashboard/DashStep2PortfolioBuild.vue'
import DashStep3Workbench from '../../components/dashboard/DashStep3Workbench.vue'
import { fetchTickerGraph } from '../../api/portfolio'
import { seedToGraphData } from '../../utils/graphAdapter'
import { setLastPortfolio } from '../../store/lastPortfolio'

const router = useRouter()

const stepNames = ['Screening', 'Portfolio Build', 'Workbench']

// Layout state
const viewMode = ref('split') // graph | split | workbench

// Step / workflow state
const currentStep = ref(1)
const candidateTickers = ref([])
const portfolio = ref(null)
const activeTicker = ref('')

// Graph pane state
const graphData = ref(null)
const graphLoading = ref(false)
const error = ref('')

const graphEmptyText = computed(() => {
  if (currentStep.value === 1) return 'Click a candidate row to preview its entity graph.'
  if (currentStep.value === 2) return 'Build a portfolio to see its top holding\'s entity graph.'
  return 'Pick a ticker above to load its entity graph.'
})

// Logs
const systemLogs = ref([])
const logContent = ref(null)

const addLog = (msg) => {
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  systemLogs.value.push({ time, msg })
  if (systemLogs.value.length > 200) systemLogs.value.shift()
  nextTick(() => {
    if (logContent.value) logContent.value.scrollTop = logContent.value.scrollHeight
  })
}

// --- Layout ---
const leftPanelStyle = computed(() => {
  if (viewMode.value === 'graph') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'workbench') return { width: '0%', opacity: 0, transform: 'translateX(-20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

const rightPanelStyle = computed(() => {
  if (viewMode.value === 'workbench') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'graph') return { width: '0%', opacity: 0, transform: 'translateX(20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

const toggleMaximize = (target) => {
  viewMode.value = viewMode.value === target ? 'split' : target
}

// --- Status ---
const statusClass = computed(() => {
  if (error.value) return 'error'
  if (graphLoading.value) return 'processing'
  return 'completed'
})
const statusText = computed(() => {
  if (error.value) return 'Error'
  if (graphLoading.value) return 'Loading Graph'
  return 'Ready'
})

// --- Graph loading ---
async function loadTickerGraph(ticker) {
  if (!ticker) return
  graphLoading.value = true
  error.value = ''
  addLog(`Fetching entity graph for ${ticker}...`)
  try {
    const seed = await fetchTickerGraph({ ticker })
    graphData.value = seedToGraphData(seed)
    addLog(`Graph loaded for ${ticker}: ${seed.entities.length} entities.`)
  } catch (err) {
    error.value = err.message || 'Failed to load graph'
    addLog(`Error loading graph for ${ticker}: ${error.value}`)
  } finally {
    graphLoading.value = false
  }
}

function onTickerFocus(ticker) {
  if (!ticker || ticker === activeTicker.value) return
  activeTicker.value = ticker
  loadTickerGraph(ticker)
}

// --- Step transitions ---
function handleStep1Continue(tickers) {
  candidateTickers.value = tickers
  currentStep.value = 2
  addLog(`Continuing to Portfolio Build with ${tickers.length} candidates: ${tickers.join(', ')}`)
}

function handleStep2Built(result) {
  portfolio.value = result
  setLastPortfolio(result)
  currentStep.value = 3
  addLog('Portfolio built. Continuing to Workbench.')

  const topTicker = Object.keys(result.weights || {})[0]
  if (topTicker) onTickerFocus(topTicker)
}

function handleGoBack() {
  if (currentStep.value > 1) {
    currentStep.value--
    addLog(`Returned to step ${currentStep.value}: ${stepNames[currentStep.value - 1]}`)
  }
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

/* Header (copied from MainView.vue for visual parity) */
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
  font-family: 'JetBrains Mono', monospace;
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
  transition: all 0.2s;
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

.final-portfolio-link {
  font-size: 12px;
  font-weight: 700;
  color: #000;
  text-decoration: underline;
}

.workflow-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.step-num {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  color: #999;
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

.status-indicator.processing .dot { background: #FF5722; animation: pulse 1s infinite; }
.status-indicator.completed .dot { background: #4CAF50; }
.status-indicator.error .dot { background: #F44336; }

@keyframes pulse { 50% { opacity: 0.5; } }

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
}

.panel-wrapper.left {
  border-right: 1px solid #EAEAEA;
}

/* Terminal (adapted from Step3Simulation.vue's system-logs, shared across steps) */
.system-logs {
  flex-shrink: 0;
  height: 140px;
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
  font-family: 'JetBrains Mono', monospace;
  letter-spacing: 0.05em;
}

.log-content {
  flex: 1;
  overflow-y: auto;
  padding: 6px 16px;
}

.log-line {
  font-family: 'JetBrains Mono', monospace;
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
