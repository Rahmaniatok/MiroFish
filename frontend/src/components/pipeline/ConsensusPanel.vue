<template>
  <div class="cs">
    <div v-if="!ready" class="cs-wait">Waits for the persona JSON (step 04).</div>

    <template v-else>
      <div class="cs-controls">
        <label class="cs-field">
          <span>Top n</span>
          <div class="stepper">
            <button @click="n = Math.max(1, n - 1)" :disabled="locked">−</button>
            <b>{{ n }}</b>
            <button @click="n = Math.min(maxN, n + 1)" :disabled="locked">+</button>
          </div>
        </label>
        <label class="cs-field">
          <span>Min votes</span>
          <div class="seg">
            <button v-for="v in [1, 2, 3]" :key="v" :class="{ on: minVotes === v }" :disabled="locked" @click="minVotes = v">{{ v }}+</button>
          </div>
        </label>
        <button class="action-btn" :disabled="busy || locked || !dirty" @click="build">
          {{ locked ? 'Locked by step 06' : result ? (dirty ? 'Recompute' : 'Up to date') : 'Build consensus' }} →
        </button>
      </div>
      <p class="cs-rule">1 persona = 1 vote · ranked by votes · ties at the cutoff all included · no weighting</p>
      <div v-if="error" class="cs-error">{{ error }}</div>

      <template v-if="result">
        <div class="cs-pick">
          <div v-for="t in picked" :key="t.ticker" class="pick">
            <span class="pick-sym">{{ t.ticker }}</span>
            <span class="pick-votes">{{ t.votes }}<small>/{{ result.persona_count }}</small></span>
          </div>
          <div v-if="!picked.length" class="cs-none">No ticker reaches {{ result.min_votes }}+ votes.</div>
        </div>
        <div class="cs-flags">
          <span v-if="result.tie_extended" class="flag">tie at {{ result.cutoff_votes }} votes → {{ picked.length }} tickers for n={{ result.n }}</span>
          <span v-if="result.fewer_than_n" class="flag warn">only {{ picked.length }} ticker(s) reach {{ result.min_votes }}+ votes (n={{ result.n }})</span>
        </div>

        <div class="matrix">
          <div class="m-head">
            <span class="m-tk">TICKER</span>
            <span v-for="p in result.personas" :key="p" class="m-p" :title="p">{{ abbr(p) }}</span>
            <span class="m-v">VOTES</span>
          </div>
          <template v-for="(row, i) in rows" :key="row.ticker">
            <div v-if="i > 0 && rows[i - 1].selected && !row.selected" class="m-cut">
              <span>cutoff · {{ result.cutoff_votes }}+ votes</span>
            </div>
            <div class="m-row" :class="{ sel: row.selected }">
              <span class="m-tk">{{ row.ticker }}</span>
              <span v-for="p in result.personas" :key="p" class="m-cell" :class="{ on: row.personas.includes(p) }" :title="`${p}${row.personas.includes(p) ? ' votes ' + row.ticker : ''}`"></span>
              <span class="m-v">
                <span class="bar"><span :style="{ width: (row.votes / result.persona_count * 100) + '%' }"></span></span>
                <b>{{ row.votes }}</b>
              </span>
            </div>
          </template>
        </div>
        <button v-if="zeroCount" class="link" @click="showZero = !showZero">
          {{ showZero ? 'Hide' : 'Show' }} {{ zeroCount }} ticker(s) with no votes
        </button>
        <p class="api-note">uploads/pipeline_runs/{{ runId }}/07_consensus.json · {{ result.generated_at?.replace('T', ' ') }}</p>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { buildConsensus, getConsensus } from '../../api/pipeline'

const props = defineProps({
  runId: { type: String, required: true },
  run: { type: Object, default: null }
})
const emit = defineEmits(['refresh-run'])

const result = ref(null)
const n = ref(5)
const minVotes = ref(2)
const busy = ref(false)
const error = ref('')
const showZero = ref(false)

const ready = computed(() => props.run?.steps?.report_json?.status === 'completed')
const locked = computed(() => props.run?.steps?.performance?.status === 'completed')
const maxN = computed(() => props.run?.universe?.ticker_universe?.length || 50)
const dirty = computed(() => !result.value || result.value.n !== n.value || result.value.min_votes !== minVotes.value)
const picked = computed(() => (result.value?.ranking || []).filter(r => r.selected))
const rows = computed(() => (result.value?.ranking || []).filter(r => showZero.value || r.votes > 0))
const zeroCount = computed(() => (result.value?.ranking || []).filter(r => r.votes === 0).length)

// "Value Investor" -> VAL, "ESG/Sustainability Investor" -> ESG
const abbr = (p) => p.split(/[\s/]/)[0].slice(0, 3).toUpperCase()

const load = async () => {
  try {
    const data = (await getConsensus(props.runId)).data
    result.value = data.result
    n.value = data.result?.n ?? data.default_n
    minVotes.value = data.result?.min_votes ?? data.default_min_votes
  } catch (e) {
    error.value = e.message
  }
}

const build = async () => {
  busy.value = true
  error.value = ''
  try {
    await buildConsensus(props.runId, n.value, minVotes.value)
    await load()
    emit('refresh-run')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

watch(ready, (r) => { if (r) load() }, { immediate: true })
</script>

<style scoped>
.cs-wait { font-size: 12px; color: #AAA; }
.cs-controls { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
.cs-field { display: flex; flex-direction: column; gap: 5px; font-size: 10px; font-weight: 700; color: #999; font-family: 'JetBrains Mono', monospace; }
.stepper { display: flex; align-items: center; border: 1px solid #E5E5E5; border-radius: 5px; overflow: hidden; }
.stepper button { border: none; background: #F7F7F7; width: 28px; height: 30px; font-size: 15px; cursor: pointer; }
.stepper button:hover:not(:disabled) { background: #FF5722; color: #FFF; }
.stepper b { min-width: 34px; text-align: center; font-size: 14px; color: #000; }
.seg { display: flex; background: #F4F4F4; padding: 3px; border-radius: 5px; gap: 2px; }
.seg button { border: none; background: none; padding: 5px 10px; border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; color: #777; cursor: pointer; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.action-btn {
  margin-left: auto; background: #000; color: #FFF; border: none; padding: 9px 14px; border-radius: 5px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; cursor: pointer; white-space: nowrap;
}
.action-btn:hover:not(:disabled) { background: #FF5722; }
.action-btn:disabled { background: #D5D5D5; cursor: default; }
.cs-rule { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #AAA; margin: 8px 0 12px; }
.cs-error { font-size: 12px; color: #C62828; background: #FFEBEE; padding: 8px 10px; border-radius: 5px; margin-bottom: 10px; }

.cs-pick { display: flex; flex-wrap: wrap; gap: 8px; }
.pick {
  display: flex; align-items: baseline; gap: 8px; background: #000; color: #FFF; border-radius: 7px; padding: 10px 14px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.08);
}
.pick-sym { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 17px; }
.pick-votes { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 13px; color: #FF8A65; }
.pick-votes small { color: #777; font-size: 10px; }
.cs-none { font-size: 12px; color: #999; }
.cs-flags { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0 12px; }
.flag { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; font-weight: 700; background: #FFF1EC; color: #D84315; padding: 4px 8px; border-radius: 4px; }
.flag.warn { background: #FFF8E1; color: #8D6E00; }

.matrix { border: 1px solid #F0F0F0; border-radius: 6px; overflow: hidden; }
.m-head, .m-row { display: grid; grid-template-columns: 70px repeat(8, 1fr) 110px; align-items: center; gap: 4px; padding: 0 10px; }
.m-head { height: 30px; background: #FAFAFA; font-family: 'JetBrains Mono', monospace; font-size: 9px; font-weight: 800; color: #AAA; }
.m-p { text-align: center; cursor: help; }
.m-row { height: 28px; border-top: 1px solid #F6F6F6; }
.m-row.sel { background: #FFF8F5; }
.m-tk { font-family: 'JetBrains Mono', monospace; font-weight: 800; font-size: 11.5px; }
.m-row:not(.sel) .m-tk { color: #888; }
.m-cell { height: 16px; border-radius: 3px; background: #F3F3F3; margin: 0 auto; width: 100%; max-width: 34px; }
.m-cell.on { background: #FF5722; }
.m-row:not(.sel) .m-cell.on { background: #FFAB91; }
.m-v { display: flex; align-items: center; gap: 6px; justify-content: flex-end; }
.m-v b { font-family: 'JetBrains Mono', monospace; font-size: 11px; min-width: 12px; text-align: right; }
.bar { flex: 1; height: 5px; background: #F0F0F0; border-radius: 3px; overflow: hidden; }
.bar span { display: block; height: 100%; background: #000; }
.m-cut { position: relative; height: 16px; border-top: 2px dashed #FF5722; }
.m-cut span {
  position: absolute; top: -9px; right: 10px; background: #FFF; padding: 0 6px;
  font-family: 'JetBrains Mono', monospace; font-size: 9px; font-weight: 800; color: #FF5722;
}
.link { background: none; border: none; color: #FF5722; font-size: 11px; font-weight: 600; cursor: pointer; margin-top: 8px; padding: 0; }
.api-note { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #999; margin-top: 10px; }
</style>
