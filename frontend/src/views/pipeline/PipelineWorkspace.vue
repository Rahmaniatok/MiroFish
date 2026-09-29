<template>
  <div class="main-view">
    <header class="app-header">
      <div class="header-left">
        <div class="brand" @click="router.push('/pipeline')">MIROFISH</div>
        <span class="back-link" @click="router.push('/pipeline')">← All runs</span>
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
        <div class="workflow-step" v-if="run">
          <span class="step-num">Step {{ uiStep }}/{{ PIPELINE_STEPS.length }}</span>
          <span class="step-name">{{ PIPELINE_STEPS[uiStep - 1]?.title }}</span>
        </div>
        <div class="step-divider"></div>
        <span class="status-indicator" :class="statusClass">
          <span class="dot"></span>
          {{ statusText }}
        </span>
      </div>
    </header>

    <main class="content-area">
      <div class="panel-wrapper left" :style="leftPanelStyle">
        <GraphPanel
          :graphData="graphData"
          :loading="loading"
          :currentPhase="2"
          :edgeLabelsDefault="false"
          @refresh="refreshAll"
          @toggle-maximize="toggleMaximize('graph')"
        />
      </div>
      <div class="panel-wrapper right" :style="rightPanelStyle">
        <PipelineWorkbench :runId="runId" :run="run" :logs="logs" @refresh-run="loadRun" />
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import GraphPanel from '../../components/GraphPanel.vue'
import PipelineWorkbench from '../../components/pipeline/PipelineWorkbench.vue'
import { PIPELINE_STEPS, uiCurrentStep } from '../../components/pipeline/pipelineSteps'
import { universeToGraphData } from '../../utils/universeGraph'
import { getRun, getRunLog } from '../../api/pipeline'
import { getGraphData } from '../../api/graph'

const props = defineProps({ runId: { type: String, required: true } })
const router = useRouter()

const viewMode = ref('split')
const run = ref(null)
const logs = ref([])
const loading = ref(false)
const error = ref('')

// Universe (sector / tier) graph until MiroFish has built the Zep graph, then the real one.
const uiStep = computed(() => uiCurrentStep(run.value))
const zepGraph = ref(null)
const graphData = computed(() => zepGraph.value || universeToGraphData(run.value?.universe))
const graphId = computed(() => run.value?.steps?.simulation?.summary?.stages?.graph === 'completed'
  ? run.value?.links?.graph_id : null)

const loadZepGraph = async () => {
  if (!graphId.value) return
  try {
    const res = await getGraphData(graphId.value)
    zepGraph.value = res.data
  } catch (e) {
    logs.value = [...logs.value, { time: new Date().toISOString(), event: 'graph_error', message: e.message }]
  }
}
watch(graphId, (id, old) => { if (id && id !== old) loadZepGraph() })

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

const statusClass = computed(() => {
  if (error.value || run.value?.status === 'failed') return 'error'
  if (run.value?.status === 'completed') return 'completed'
  return 'processing'
})
const statusText = computed(() => {
  if (error.value) return 'Error'
  if (!run.value) return 'Loading'
  return { in_progress: 'In progress', completed: 'Completed', failed: 'Failed' }[run.value.status] || run.value.status
})

const refreshAll = async () => {
  await loadRun()
  await loadZepGraph()
}

const toggleMaximize = (target) => {
  viewMode.value = viewMode.value === target ? 'split' : target
}

const loadRun = async () => {
  loading.value = true
  try {
    const [r, l] = await Promise.all([getRun(props.runId), getRunLog(props.runId)])
    run.value = r.data
    logs.value = l.data
    error.value = ''
  } catch (e) {
    error.value = e.message
    logs.value = [...logs.value, { time: new Date().toISOString(), event: 'error', message: e.message }]
  } finally {
    loading.value = false
  }
}

// while MiroFish runs (in another tab or later today), keep the card in sync
let timer = null
const mirofishActive = computed(() => {
  const s = run.value?.steps
  return s?.prompt?.status === 'completed' && s?.simulation?.status !== 'completed'
})
const schedule = () => {
  clearTimeout(timer)
  timer = setTimeout(async () => {
    if (mirofishActive.value && !document.hidden) await loadRun()
    schedule()
  }, 5000)
}

onMounted(async () => {
  await loadRun()
  schedule()
})
onUnmounted(() => clearTimeout(timer))
</script>

<style scoped>
.main-view {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #FFF;
  overflow: hidden;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}
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
}
.header-left { display: flex; align-items: center; gap: 16px; }
.header-center { position: absolute; left: 50%; transform: translateX(-50%); }
.brand {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 18px;
  letter-spacing: 1px;
  cursor: pointer;
}
.back-link { font-size: 12px; color: #999; cursor: pointer; font-family: 'JetBrains Mono', monospace; }
.back-link:hover { color: #000; }
.view-switcher { display: flex; background: #F5F5F5; padding: 4px; border-radius: 6px; gap: 4px; }
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
}
.switch-btn.active { background: #FFF; color: #000; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
.header-right { display: flex; align-items: center; gap: 16px; }
.workflow-step { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.step-num { font-family: 'JetBrains Mono', monospace; font-weight: 700; color: #999; }
.step-name { font-weight: 700; color: #000; }
.step-divider { width: 1px; height: 14px; background-color: #E0E0E0; }
.status-indicator { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #666; font-weight: 500; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #CCC; }
.status-indicator.processing .dot { background: #FF5722; animation: pulse 1s infinite; }
.status-indicator.completed .dot { background: #4CAF50; }
.status-indicator.error .dot { background: #F44336; }
@keyframes pulse { 50% { opacity: 0.5; } }
.content-area { flex: 1; display: flex; position: relative; overflow: hidden; }
.panel-wrapper {
  height: 100%;
  overflow: hidden;
  transition: width 0.4s cubic-bezier(0.25, 0.8, 0.25, 1), opacity 0.3s ease, transform 0.3s ease;
  will-change: width, opacity, transform;
}
.panel-wrapper.left { border-right: 1px solid #EAEAEA; }
</style>
