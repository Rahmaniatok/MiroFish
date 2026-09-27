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
            <span v-else-if="statusOf(step.key) === 'failed'" class="badge error">Failed</span>
            <span v-else-if="i + 1 === run?.current_step" class="badge accent">Next</span>
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

          <!-- ===== Not built yet ===== -->
          <template v-else-if="statusOf(step.key) === 'pending'">
            <div v-if="i + 1 === run?.current_step" class="next-box">
              <span>This step is next. It will be available once it is built.</span>
              <button class="action-btn" disabled>Run step {{ String(i + 1).padStart(2, '0') }} →</button>
            </div>
          </template>
          <div v-if="run?.steps?.[step.key]?.error" class="note error">{{ run.steps[step.key].error }}</div>
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
import { PIPELINE_STEPS } from './pipelineSteps'
import { formatMarketCap } from '../../utils/universeGraph'

const props = defineProps({
  runId: { type: String, required: true },
  run: { type: Object, default: null },
  logs: { type: Array, default: () => [] }
})

const logContent = ref(null)

const universe = computed(() => props.run?.universe || null)
const summary = computed(() => props.run?.steps?.universe?.summary || {})
const newsWindow = computed(() => {
  if (!universe.value) return '—'
  const d = new Date(universe.value.as_of_date)
  d.setDate(d.getDate() - 90)
  return `${d.toISOString().slice(0, 10)} → ${universe.value.as_of_date}`
})

const statusOf = (key) => props.run?.steps?.[key]?.status || 'pending'
const cardClass = (key, num) => ({
  completed: statusOf(key) === 'completed',
  active: statusOf(key) === 'running' || (statusOf(key) === 'pending' && num === props.run?.current_step),
  failed: statusOf(key) === 'failed',
  dim: statusOf(key) === 'pending' && num !== props.run?.current_step
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
