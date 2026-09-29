<template>
  <div class="rj">
    <div v-if="!reportReady" class="rj-wait">Waits for the MiroFish report (step 03).</div>

    <template v-else>
      <div class="rj-bar" :class="state">
        <span v-if="state === 'running'" class="spin"></span>
        <span class="rj-msg">{{ message }}</span>
        <button class="action-btn" :disabled="state === 'running' || busy" @click="start">
          {{ result ? 'Re-run conversion' : 'Convert report to JSON' }} →
        </button>
      </div>

      <div v-if="result" class="rj-table">
        <div v-for="p in result.personas" :key="p.persona" class="rj-row">
          <span class="rj-persona">{{ p.persona }}</span>
          <span class="rj-count" :class="{ low: p.tickers.length < 5 }">{{ p.tickers.length }}</span>
          <span class="rj-tickers">
            <span v-for="t in p.tickers" :key="t" class="rj-chip">{{ t }}</span>
            <span v-if="!p.tickers.length" class="rj-none">no picks</span>
            <span v-for="t in dropped(p.persona)" :key="'x' + t.ticker" class="rj-chip dropped" :title="t.why">{{ t.ticker }}</span>
          </span>
        </div>
      </div>

      <details v-if="meta?.warnings?.length" class="rj-details">
        <summary>{{ meta.warnings.length }} validation note(s)</summary>
        <ul><li v-for="w in meta.warnings" :key="w">{{ w }}</li></ul>
      </details>
      <details v-if="result" class="rj-details">
        <summary>View JSON</summary>
        <pre>{{ JSON.stringify(result, null, 2) }}</pre>
      </details>
      <p v-if="meta" class="api-note">
        uploads/pipeline_runs/{{ runId }}/06_report.json · from {{ meta.report_id }} · {{ meta.model }} · {{ meta.generated_at?.replace('T', ' ') }}
      </p>
    </template>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { getReportJson, startReportJson } from '../../api/pipeline'

const props = defineProps({
  runId: { type: String, required: true },
  run: { type: Object, default: null }
})
const emit = defineEmits(['refresh-run'])

const data = ref(null)
const busy = ref(false)
const error = ref('')
let timer = null

const reportReady = computed(() => props.run?.steps?.simulation?.status === 'completed')
const state = computed(() => (error.value ? 'failed' : data.value?.status) || 'pending')
const result = computed(() => data.value?.result || null)
const meta = computed(() => data.value?.meta || null)
const message = computed(() => {
  if (error.value) return error.value
  switch (state.value) {
    case 'running': return 'The LLM is reading the report…'
    case 'completed': return `${result.value?.personas.reduce((n, p) => n + p.tickers.length, 0)} picks over ${result.value?.personas.filter(p => p.tickers.length).length}/8 personas`
    case 'failed': return `Failed: ${data.value?.error}`
    case 'interrupted': return 'Interrupted by a backend restart — run it again.'
    default: return 'Turn the MiroFish report into the persona JSON with the LLM from .env.'
  }
})

// tickers the validator removed, shown struck through with the reason
const dropped = (persona) => {
  const v = meta.value?.validation?.[persona]
  if (!v) return []
  return [
    ...v.dropped_out_of_universe.map(t => ({ ticker: t, why: 'not in ticker_universe' })),
    ...v.dropped_not_in_report.map(t => ({ ticker: t, why: 'never mentioned in the report' }))
  ]
}

const load = async () => {
  try {
    const before = data.value?.status
    data.value = (await getReportJson(props.runId)).data
    error.value = ''
    if (before === 'running' && data.value.status !== 'running') emit('refresh-run')
  } catch (e) {
    error.value = e.message
  }
  clearTimeout(timer)
  if (data.value?.status === 'running') timer = setTimeout(load, 3000)
}

const start = async () => {
  busy.value = true
  error.value = ''
  try {
    data.value = (await startReportJson(props.runId)).data
    emit('refresh-run')
    load()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

watch(reportReady, (ready) => { if (ready) load() }, { immediate: true })
onUnmounted(() => clearTimeout(timer))
</script>

<style scoped>
.rj-wait { font-size: 12px; color: #AAA; }
.rj-bar {
  display: flex; align-items: center; gap: 10px; background: #F9F9F9; border: 1px dashed #DDD;
  border-radius: 6px; padding: 10px 12px; font-size: 12px; color: #555;
}
.rj-bar.completed { background: #F6FBF6; border-color: #A5D6A7; color: #2E7D32; }
.rj-bar.running { background: #FFF6F3; border-color: #FFB199; color: #A33B17; }
.rj-bar.failed, .rj-bar.interrupted { background: #FFEBEE; border-color: #EF9A9A; color: #C62828; }
.rj-msg { flex: 1; }
.action-btn {
  background: #000; color: #FFF; border: none; padding: 8px 14px; border-radius: 5px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; white-space: nowrap; cursor: pointer;
}
.action-btn:hover:not(:disabled) { background: #FF5722; }
.action-btn:disabled { background: #CCC; cursor: not-allowed; }
.spin { width: 12px; height: 12px; border: 2px solid #FFD0C0; border-top-color: #FF5722; border-radius: 50%; animation: spin 0.7s linear infinite; }

.rj-table { margin-top: 12px; border: 1px solid #F0F0F0; border-radius: 6px; overflow: hidden; }
.rj-row { display: grid; grid-template-columns: 190px 28px 1fr; align-items: center; gap: 8px; padding: 7px 10px; border-bottom: 1px solid #F5F5F5; }
.rj-row:last-child { border-bottom: none; }
.rj-persona { font-size: 12px; font-weight: 600; }
.rj-count { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; text-align: center; background: #000; color: #FFF; border-radius: 3px; padding: 2px 0; }
.rj-count.low { background: #FFE0B2; color: #8D5A00; }
.rj-tickers { display: flex; flex-wrap: wrap; gap: 4px; }
.rj-chip { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 800; padding: 3px 7px; border-radius: 3px; background: #FFF1EC; color: #D84315; border: 1px solid #FFCCBC; }
.rj-chip.dropped { background: #F5F5F5; color: #AAA; border-color: #EEE; text-decoration: line-through; cursor: help; }
.rj-none { font-size: 11px; color: #BBB; font-style: italic; }
.rj-details { margin-top: 8px; font-size: 11.5px; color: #666; }
.rj-details summary { cursor: pointer; font-weight: 600; color: #FF5722; padding: 3px 0; }
.rj-details ul { margin: 4px 0 0 18px; line-height: 1.6; }
.rj-details pre {
  margin-top: 6px; background: #0B0B0B; color: #DDD; border-radius: 6px; padding: 12px 14px;
  font-family: 'JetBrains Mono', monospace; font-size: 10.5px; max-height: 300px; overflow: auto;
}
.api-note { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #999; margin-top: 10px; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
