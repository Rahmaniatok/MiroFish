<template>
  <div class="workbench-panel">
    <div class="wb-head">
      <div class="wb-title">
        <span class="run-name">{{ run?.name || 'Loading run…' }}</span>
        <span class="run-id">{{ runId }}</span>
      </div>
      <PipelineStepRail v-if="run" :steps="run.steps" />
    </div>

    <div class="scroll-container">
      <div
        v-for="(step, i) in PIPELINE_STEPS"
        :key="step.key"
        class="step-card"
        :class="cardClass(step.key, i + 1)"
      >
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">{{ String(i + 1).padStart(2, '0') }}</span>
            <span class="step-title">{{ step.title }}</span>
          </div>
          <div class="step-status">
            <span v-if="statusOf(step.key) === 'completed'" class="badge success">Completed</span>
            <span v-else-if="statusOf(step.key) === 'running'" class="badge processing">Running</span>
            <span v-else-if="statusOf(step.key) === 'paused'" class="badge accent">Paused</span>
            <span v-else-if="statusOf(step.key) === 'failed'" class="badge error">Failed</span>
            <span v-else-if="i + 1 === uiCurrent" class="badge accent">Next</span>
            <span v-else class="badge pending">Pending</span>
          </div>
        </div>

        <div class="card-content">
          <p class="description">{{ step.desc }}</p>

          <!-- ===== Step 01 detail ===== -->
          <template v-if="step.key === 'universe' && universe">
            <div class="kv-grid">
              <div class="kv"><span class="k">AS_OF</span><span class="v">{{ universe.as_of_date }}</span></div>
              <div class="kv"><span class="k">TICKERS</span><span class="v">{{ universe.ticker_universe.length }}</span></div>
              <div class="kv"><span class="k">TOTAL CAP</span><span class="v">{{ formatMarketCap(summary.total_market_cap) }}</span></div>
              <div class="kv"><span class="k">NEWS WINDOW</span><span class="v">{{ newsWindow }}</span></div>
            </div>

            <div class="meta-line">
              <span class="tag-label">SECTORS</span>
              <span v-for="(n, s) in summary.by_sector" :key="s" class="entity-tag">{{ s }} <b>{{ n }}</b></span>
            </div>
            <div class="meta-line">
              <span class="tag-label">TIERS</span>
              <span v-for="(n, t) in summary.by_tier" :key="t" class="entity-tag" :class="`tier-${t}`">{{ t }} <b>{{ n }}</b></span>
            </div>

            <span class="tag-label">TICKER_UNIVERSE</span>
            <div class="ticker-chips">
              <span
                v-for="row in universe.universe"
                :key="row.ticker"
                class="ticker-chip"
                :title="`${row.company_name} · ${formatMarketCap(row.market_cap)}`"
              >
                <i :class="`tier-${row.market_cap_tier}`"></i>{{ row.ticker }}
              </span>
            </div>

            <div v-if="universe.excluded_by_user?.length" class="note">
              Excluded by you: {{ universe.excluded_by_user.join(', ') }}
            </div>
            <div v-if="universe.skipped?.length" class="note">
              No market data (skipped): {{ universe.skipped.map(s => s.ticker).join(', ') }}
            </div>
            <div v-for="w in universe.warnings" :key="w" class="note warn">⚠ {{ w }}</div>
            <details class="caveats">
              <summary>Data caveats</summary>
              <p v-for="c in universe.caveats" :key="c">{{ c }}</p>
            </details>
            <p class="api-note">uploads/pipeline_runs/{{ runId }}/01_universe.json</p>
          </template>

          <!-- ===== Step 02 detail ===== -->
          <template v-else-if="step.key === 'news' && run">
            <div v-if="newsSummary" class="kv-grid">
              <div class="kv"><span class="k">RAW</span><span class="v">{{ newsSummary.raw?.toLocaleString() }}</span></div>
              <div class="kv"><span class="k">RELEVANT</span><span class="v">{{ newsSummary.relevant?.toLocaleString() }}</span></div>
              <div class="kv"><span class="k">IN TXT</span><span class="v">{{ newsSummary.kept }} <small>(cap {{ newsSummary.cap }})</small></span></div>
              <div class="kv"><span class="k">TXT SIZE</span><span class="v">{{ Math.round((newsSummary.bytes || 0) / 1024) }} KB</span></div>
            </div>
            <div class="next-box" :class="{ done: statusOf('news') === 'completed' }">
              <span v-if="seedDone">txt_berita is locked — it is the reality seed of the MiroFish run.</span>
              <span v-else-if="statusOf('news') === 'completed'">txt_berita is ready. Review it and use it as the reality seed in the News Room.</span>
              <span v-else-if="statusOf('news') === 'pending'">Fetch 90 days of Finnhub news for every ticker.</span>
              <span v-else>News fetch is {{ statusOf('news') }} — continue in the News Room.</span>
              <router-link class="action-btn" :to="`/pipeline/${runId}/news`">Open News Room →</router-link>
            </div>
            <p v-if="newsSummary" class="api-note">uploads/pipeline_runs/{{ runId }}/02_news/txt_berita.txt</p>
          </template>

          <!-- ===== Step 03 detail: reality seed / prompt / simulation ===== -->
          <template v-else-if="step.key === 'mirofish' && run">
            <div class="sub-steps">
              <div class="sub" :class="run.steps.seed.status">
                <span class="sub-dot"></span>
                <span class="sub-name">Reality seed</span>
                <span v-if="seedSummary" class="sub-val">
                  {{ seedSummary.filename }} · {{ Math.round((seedSummary.bytes || 0) / 1024) }} KB → {{ seedSummary.project_id }}
                </span>
                <router-link v-else-if="statusOf('news') === 'completed'" class="sub-link" :to="`/pipeline/${runId}/news`">feed from News Room →</router-link>
                <span v-else class="sub-val muted">waits for txt_berita</span>
              </div>
              <div class="sub" :class="run.steps.prompt.status">
                <span class="sub-dot"></span>
                <span class="sub-name">Simulation prompt</span>
                <span v-if="promptSummary" class="sub-val">
                  {{ promptSummary.news_label }} · {{ (promptSummary.chars || 0).toLocaleString() }} chars → {{ promptSummary.project_id }}
                </span>
                <span v-else class="sub-val muted">built from ticker_universe after the seed</span>
              </div>
              <details v-if="promptText" class="prompt-box">
                <summary>View simulation prompt</summary>
                <pre>{{ promptText }}</pre>
              </details>
              <div v-for="st in mirofishStages" :key="st.key" class="sub" :class="st.status">
                <span class="sub-dot"></span>
                <span class="sub-name">{{ st.label }}</span>
                <span class="sub-val" :class="{ muted: st.status === 'pending' }">{{ st.detail }}</span>
              </div>
            </div>
            <div v-if="run.steps.prompt.status === 'completed'" class="next-box" :class="{ done: statusOf('mirofish') === 'completed' }">
              <span>{{ mirofishHint }}</span>
              <button class="action-btn" :disabled="mirofishTarget.disabled || reportBusy" @click="openMirofish">
                {{ reportBusy ? 'Starting report…' : mirofishTarget.label }}<template v-if="!mirofishTarget.disabled"> →</template>
              </button>
            </div>
          </template>

          <!-- ===== Step 04: report -> JSON ===== -->
          <ReportJsonPanel
            v-else-if="step.key === 'report_json' && run"
            :runId="runId"
            :run="run"
            @refresh-run="$emit('refresh-run')"
          />

          <!-- ===== Step 05: consensus ===== -->
          <ConsensusPanel
            v-else-if="step.key === 'consensus' && run"
            :runId="runId"
            :run="run"
            @refresh-run="$emit('refresh-run')"
          />

          <!-- ===== Step 06: performance ===== -->
          <template v-else-if="step.key === 'performance' && run">
            <div v-if="perfSummary" class="kv-grid">
              <div class="kv"><span class="k">PERIOD</span><span class="v">{{ perfSummary.base_date }} → {{ perfSummary.end_date }}</span></div>
              <div class="kv"><span class="k">CONSENSUS</span><span class="v" :class="perfSummary.consensus_return >= 0 ? 'up' : 'down'">{{ fmtPct(perfSummary.consensus_return) }}</span></div>
              <div class="kv"><span class="k">S&amp;P 500</span><span class="v">{{ fmtPct(perfSummary.spy_return) }}</span></div>
              <div class="kv"><span class="k">EXCESS</span><span class="v" :class="perfSummary.consensus_return - perfSummary.spy_return >= 0 ? 'up' : 'down'">{{ fmtPct(perfSummary.consensus_return - perfSummary.spy_return) }}</span></div>
            </div>
            <div class="next-box" :class="{ done: !!perfSummary }">
              <span v-if="perfSummary">Metrics, charts, per-persona leaderboard and correlation are in the report.</span>
              <span v-else-if="statusOf('consensus') === 'completed'">Measure the consensus and every persona's picks from as_of until today.</span>
              <span v-else>Waits for the consensus (step 05).</span>
              <router-link v-if="statusOf('consensus') === 'completed'" class="action-btn" :to="`/pipeline/${runId}/performance`">Open Performance Report →</router-link>
            </div>
          </template>

          <!-- ===== Not built yet ===== -->
          <template v-else-if="statusOf(step.key) === 'pending'">
            <div v-if="i + 1 === uiCurrent" class="next-box">
              <span>This step is next. It will be available once it is built.</span>
              <button class="action-btn" disabled>Run step {{ String(i + 1).padStart(2, '0') }} →</button>
            </div>
          </template>
          <div v-if="uiStepError(run?.steps, step)" class="note error">{{ uiStepError(run?.steps, step) }}</div>
        </div>
      </div>
    </div>

    <!-- Persistent run history (run.log) -->
    <div class="system-logs">
      <div class="log-header">
        <span class="log-title">RUN HISTORY · run.log</span>
        <span class="log-id">{{ runId }}</span>
      </div>
      <div class="log-content" ref="logContent">
        <div class="log-line" v-for="(log, idx) in logs" :key="idx">
          <span class="log-time">{{ log.time.replace('T', ' ') }}</span>
          <span class="log-event">{{ log.event }}</span>
          <span class="log-msg">{{ log.message }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import PipelineStepRail from './PipelineStepRail.vue'
import ReportJsonPanel from './ReportJsonPanel.vue'
import ConsensusPanel from './ConsensusPanel.vue'
import { PIPELINE_STEPS, uiStepStatus, uiCurrentStep, uiStepError } from './pipelineSteps'
import { useRouter } from 'vue-router'
import { getPrompt } from '../../api/pipeline'
import { generateReport } from '../../api/report'
import { rememberPipelineRun } from '../../store/pipelineReturn'
import { formatMarketCap } from '../../utils/universeGraph'

const props = defineProps({
  runId: { type: String, required: true },
  run: { type: Object, default: null },
  logs: { type: Array, default: () => [] }
})

defineEmits(['refresh-run'])

const logContent = ref(null)

const universe = computed(() => props.run?.universe || null)
const summary = computed(() => props.run?.steps?.universe?.summary || {})
const perfSummary = computed(() => props.run?.steps?.performance?.status === 'completed' ? props.run.steps.performance.summary : null)
const fmtPct = (v) => v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
const newsSummary = computed(() => {
  const s = props.run?.steps?.news
  return s?.status === 'completed' ? s.summary : null
})
const newsWindow = computed(() => {
  if (!universe.value) return '—'
  const d = new Date(universe.value.as_of_date)
  d.setDate(d.getDate() - 90)
  return `${d.toISOString().slice(0, 10)} → ${universe.value.as_of_date}`
})

const seedSummary = computed(() => props.run?.steps?.seed?.status === 'completed' ? props.run.steps.seed.summary : null)
const seedDone = computed(() => !!seedSummary.value)
const router = useRouter()

// ---- step 5: MiroFish progress (synced from project / simulation / report files) ----
const mf = computed(() => props.run?.steps?.simulation?.summary || {})
const mirofishStages = computed(() => {
  const s = mf.value.stages || {}
  const rounds = mf.value.total_rounds ? `round ${mf.value.current_round || 0}/${mf.value.total_rounds}` : ''
  return [
    { key: 'ontology', label: 'Ontology', status: s.ontology || 'pending',
      detail: s.ontology === 'completed' ? 'generated from seed + prompt' : 'LLM reads the seed + prompt' },
    { key: 'graph', label: 'Knowledge graph (Zep)', status: s.graph || 'pending',
      detail: mf.value.graph_id || (s.graph === 'running' ? 'building…' : 'built from txt_berita chunks') },
    { key: 'simulation', label: 'Agents & simulation', status: s.simulation || 'pending',
      detail: mf.value.simulation_id ? [mf.value.simulation_id, rounds].filter(Boolean).join(' · ') : 'env setup + OASIS rounds' },
    { key: 'report', label: 'Report', status: s.report || 'pending',
      detail: mf.value.report_id ? `${mf.value.report_id} · ${mf.value.report_status}` : 'MiroFish report agent' }
  ]
})
// Where the MiroFish button goes. NEVER link to /simulation/<id>/start (or
// /simulation/<id>) once a simulation has started: those original pages
// re-run things on mount — Step3Simulation force-restarts the simulation
// (wiping its results; without ?maxRounds it runs the config's full length,
// e.g. 168 rounds) and Step2EnvSetup re-runs prepare when status != ready.
const mirofishTarget = computed(() => {
  const s = mf.value.stages || {}
  const runner = mf.value.runner_status
  const simDone = ['completed', 'stopped'].includes(runner) || s.simulation === 'completed'
  const simRunning = ['running', 'starting', 'stopping', 'paused'].includes(runner) && !simDone
  if (mf.value.report_id && mf.value.report_status !== 'failed') {
    return { label: 'Open MiroFish report', path: `/report/${mf.value.report_id}` }
  }
  if (simDone) {
    // no report yet, or the last one failed: start one via the API (same call as
    // the original "Generate Report" button) without opening the restart page
    return { label: mf.value.report_status === 'failed' ? 'Retry MiroFish report' : 'Generate MiroFish report', action: 'report' }
  }
  if (simRunning) {
    return { label: `Simulation running · round ${mf.value.current_round || 0}/${mf.value.total_rounds || '?'}`, disabled: true }
  }
  if (mf.value.simulation_id) {
    return { label: 'Open MiroFish environment setup', path: `/simulation/${mf.value.simulation_id}` }
  }
  const pid = props.run?.links?.project_id
  return { label: s.ontology === 'completed' ? 'Continue in MiroFish' : 'Start MiroFish', path: `/process/${pid}` }
})
const mirofishHint = computed(() => {
  const s = mf.value.stages || {}
  if (s.report === 'completed') return 'MiroFish report is ready — step 04 converts it to JSON.'
  if (s.report === 'failed') return `The MiroFish report failed${mf.value.error ? ' (' + mf.value.error.slice(0, 120) + ')' : ''}. The simulation is kept — retry writes a new report from it.`
  if (Object.values(s).includes('failed')) return `A MiroFish stage failed${mf.value.error ? ': ' + mf.value.error : ''}. Open MiroFish to retry.`
  if (mirofishTarget.value.disabled) return 'The simulation is running in MiroFish; this card updates by itself. Generate the report once it has finished.'
  if (!s.ontology || s.ontology === 'pending') return 'Seed and prompt are on the project. Run it in the original MiroFish UI: ontology → graph → env setup → simulation → report.'
  return 'Continue in MiroFish; progress here updates automatically.'
})
const reportBusy = ref(false)
const openMirofish = async () => {
  const target = mirofishTarget.value
  if (target.disabled) return
  rememberPipelineRun(props.runId, props.run?.name)
  if (target.action === 'report') {
    reportBusy.value = true
    try {
      const res = await generateReport({ simulation_id: mf.value.simulation_id, force_regenerate: true })
      router.push(`/report/${res.data.report_id}`)
    } catch (e) {
      alert(`Could not start the report: ${e.message}`)
    } finally {
      reportBusy.value = false
    }
    return
  }
  router.push(target.path)
}
const promptSummary = computed(() => props.run?.steps?.prompt?.status === 'completed' ? props.run.steps.prompt.summary : null)
const promptText = ref('')
watch(promptSummary, async (s) => {
  if (!s || promptText.value) return
  try {
    promptText.value = (await getPrompt(props.runId)).data.text || ''
  } catch {
    promptText.value = ''
  }
}, { immediate: true })
const uiCurrent = computed(() => uiCurrentStep(props.run))

// status of a UI step (aggregated over its backend steps)
const statusOf = (key) => uiStepStatus(props.run?.steps, PIPELINE_STEPS.find(s => s.key === key))
const cardClass = (key, num) => ({
  completed: statusOf(key) === 'completed',
  active: ['running', 'paused'].includes(statusOf(key)) || (statusOf(key) === 'pending' && num === uiCurrent.value),
  failed: statusOf(key) === 'failed',
  dim: statusOf(key) === 'pending' && num !== uiCurrent.value
})

watch(() => props.logs.length, async () => {
  await nextTick()
  if (logContent.value) logContent.value.scrollTop = logContent.value.scrollHeight
})
</script>

<style scoped>
.workbench-panel {
  height: 100%;
  background-color: #FAFAFA;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.wb-head {
  padding: 16px 24px 14px;
  background: #FFF;
  border-bottom: 1px solid #EAEAEA;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.wb-title { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.run-name { font-weight: 700; font-size: 15px; }
.run-id { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; }

.scroll-container {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.step-card {
  background: #FFF;
  border-radius: 8px;
  padding: 18px 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
  border: 1px solid #EAEAEA;
  transition: all 0.3s ease;
}
.step-card.active { border-color: #FF5722; box-shadow: 0 4px 12px rgba(255, 87, 34, 0.08); }
.step-card.failed { border-color: #F44336; }
.step-card.dim { opacity: 0.6; padding-top: 14px; padding-bottom: 14px; }
.step-card.dim .card-header { margin-bottom: 6px; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.step-info { display: flex; align-items: center; gap: 12px; }
.step-num { font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700; color: #E0E0E0; }
.step-card.active .step-num, .step-card.completed .step-num { color: #000; }
.step-title { font-weight: 600; font-size: 14px; letter-spacing: 0.5px; }
.badge { font-size: 10px; padding: 4px 8px; border-radius: 4px; font-weight: 600; text-transform: uppercase; }
.badge.success { background: #E8F5E9; color: #2E7D32; }
.badge.processing, .badge.accent { background: #FF5722; color: #FFF; }
.badge.pending { background: #F5F5F5; color: #999; }
.badge.error { background: #FFEBEE; color: #C62828; }
.description { font-size: 12px; color: #666; line-height: 1.5; margin-bottom: 12px; }
.step-card.dim .description { margin-bottom: 0; }
.api-note { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #999; margin-top: 12px; }

.kv-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-bottom: 14px;
}
.kv { background: #F9F9F9; border-radius: 5px; padding: 9px 10px; display: flex; flex-direction: column; gap: 3px; }
.k { font-family: 'JetBrains Mono', monospace; font-size: 9px; color: #AAA; font-weight: 700; }
.v { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; font-weight: 700; }
.meta-line { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 8px; }
.tag-label { font-size: 10px; color: #AAA; font-weight: 600; margin-right: 4px; display: inline-block; margin-bottom: 6px; }
.meta-line .tag-label { margin-bottom: 0; }
.entity-tag {
  background: #F5F5F5;
  border: 1px solid #EEE;
  padding: 3px 9px;
  border-radius: 4px;
  font-size: 11px;
  color: #333;
  font-family: 'JetBrains Mono', monospace;
}
.entity-tag b { color: #FF5722; margin-left: 3px; }
.ticker-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.ticker-chip {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  padding: 4px 8px 4px 6px;
  border: 1px solid #E5E5E5;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: #FFF;
}
.ticker-chip i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
i.tier-mega { background: #000; }
i.tier-large { background: #FF5722; }
i.tier-mid { background: #FFAB91; }
i.tier-small { background: #BDBDBD; }

.note { font-size: 11.5px; color: #777; margin-top: 10px; line-height: 1.5; }
.note.warn { color: #8D6E00; }
.note.error { color: #C62828; }
.caveats { margin-top: 10px; font-size: 11px; color: #888; }
.caveats summary { cursor: pointer; font-weight: 600; }
.caveats p { margin-top: 4px; line-height: 1.5; }

.next-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: #FFF6F3;
  border: 1px dashed #FFB199;
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  color: #A33B17;
}
.action-btn {
  background: #000;
  color: #FFF;
  border: none;
  padding: 8px 14px;
  border-radius: 5px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.action-btn:disabled { background: #CCC; cursor: not-allowed; }
a.action-btn { text-decoration: none; }
a.action-btn:hover { background: #FF5722; }
.next-box.done { background: #F6FBF6; border-color: #A5D6A7; color: #2E7D32; }
.v small { font-size: 10px; color: #999; }
.v.up { color: #006300; }
.v.down { color: #B3261E; }
.sub-steps { display: flex; flex-direction: column; gap: 6px; }
.sub { display: flex; align-items: center; gap: 10px; background: #F9F9F9; border-radius: 5px; padding: 8px 10px; font-size: 12px; }
.sub-dot { width: 8px; height: 8px; border-radius: 50%; background: #DDD; flex-shrink: 0; }
.sub.completed .sub-dot { background: #4CAF50; }
.sub.running .sub-dot { background: #FF5722; animation: pulse 1s infinite; }
.sub.failed .sub-dot { background: #F44336; }
.sub-name { font-weight: 600; white-space: nowrap; }
.sub-val { margin-left: auto; font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #333; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub-val.muted { color: #AAA; }
.sub-link { margin-left: auto; font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #FF5722; text-decoration: none; font-weight: 700; }
@keyframes pulse { 50% { opacity: 0.4; } }
.prompt-box { margin-top: 2px; font-size: 11.5px; }
.prompt-box summary { cursor: pointer; font-weight: 600; color: #FF5722; padding: 4px 2px; }
.prompt-box pre {
  margin-top: 6px; background: #0B0B0B; color: #DDD; border-radius: 6px; padding: 12px 14px;
  font-family: 'JetBrains Mono', monospace; font-size: 10.5px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto;
}

.system-logs {
  background: #000;
  color: #DDD;
  padding: 14px 16px;
  font-family: 'JetBrains Mono', monospace;
  border-top: 1px solid #222;
  flex-shrink: 0;
}
.log-header {
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid #333;
  padding-bottom: 8px;
  margin-bottom: 8px;
  font-size: 10px;
  color: #888;
}
.log-content { display: flex; flex-direction: column; gap: 4px; height: 96px; overflow-y: auto; padding-right: 4px; }
.log-line { font-size: 11px; display: flex; gap: 12px; line-height: 1.5; }
.log-time { color: #666; min-width: 140px; }
.log-event { color: #FF8A65; min-width: 120px; }
.log-msg { color: #CCC; word-break: break-all; }
</style>
