<template>
  <div class="step-workbench">
    <div v-if="!portfolio" class="empty-state">
      <p>No portfolio loaded yet.</p>
      <button class="btn-primary" @click="$emit('go-back')">← Back to Portfolio Build</button>
    </div>

    <template v-else>
      <div class="workbench-header">
        <button class="btn-secondary" @click="$emit('go-back')">← Back</button>

        <label class="ticker-picker">
          Ticker
          <select :value="activeTicker" @change="onTickerChange($event.target.value)">
            <option v-for="t in knownTickers" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>

        <div class="sub-tabs">
          <button class="tab-btn" :class="{ active: subTab === 'chat' }" @click="subTab = 'chat'">Chat</button>
          <button class="tab-btn" :class="{ active: subTab === 'feed' }" @click="subTab = 'feed'">Debate Feed</button>
        </div>

        <div class="debate-controls" v-if="subTab === 'feed'">
          <label>
            Rounds
            <input type="number" min="1" max="10" v-model.number="rounds" class="num-input" />
          </label>
          <button class="btn-primary" :disabled="debateLoading || !activeTicker" @click="runDebate(activeTicker)">
            {{ debateLoading ? 'Running…' : 'Run Debate' }}
          </button>
        </div>
      </div>

      <div v-if="debateError" class="error-banner">{{ debateError }}</div>
      <div v-if="debateLoading" class="loading-note">
        Running a fresh multi-round agent debate for {{ activeTicker }} — this calls the LLM once per persona per round.
      </div>

      <div class="workbench-body">
        <PortfolioChat
          v-show="subTab === 'chat'"
          :portfolio="portfolio"
          :ticker="activeTicker"
          @add-log="(m) => $emit('add-log', m)"
          @run-debate="runDebate"
        />
        <DebateFeed v-show="subTab === 'feed'" :transcript="transcript" />
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { debatePortfolio } from '../../api/portfolio'
import PortfolioChat from './PortfolioChat.vue'
import DebateFeed from './DebateFeed.vue'

const props = defineProps({
  portfolio: { type: Object, default: null },
  activeTicker: { type: String, default: '' }
})
const emit = defineEmits(['go-back', 'ticker-change', 'add-log'])

const subTab = ref('chat')
const rounds = ref(3)
const debateLoading = ref(false)
const debateError = ref('')
const transcript = ref(null)

const knownTickers = computed(() => {
  if (!props.portfolio) return []
  const out = new Set()
  Object.keys(props.portfolio.weights || {}).forEach(t => out.add(t))
  for (const h of props.portfolio.holdings || []) out.add(h.ticker)
  for (const e of props.portfolio.screening?.excluded_non_compliant || []) out.add(e.ticker)
  for (const s of props.portfolio.screening?.skipped || []) out.add(s.ticker)
  return Array.from(out).sort()
})

function onTickerChange(ticker) {
  transcript.value = null
  emit('ticker-change', ticker)
}

async function runDebate(ticker) {
  if (!ticker) return
  if (ticker !== props.activeTicker) emit('ticker-change', ticker)
  subTab.value = 'feed'
  debateLoading.value = true
  debateError.value = ''
  transcript.value = null
  emit('add-log', `Running ${rounds.value}-round debate for ${ticker}...`)
  try {
    const result = await debatePortfolio({ ticker, portfolio: props.portfolio, rounds: rounds.value })
    transcript.value = result
    emit('add-log', `Debate complete for ${ticker}.`)
  } catch (err) {
    debateError.value = err.message || 'Unknown error while running the debate.'
    emit('add-log', `Debate error: ${debateError.value}`)
  } finally {
    debateLoading.value = false
  }
}

// Reset the transcript when the caller swaps the active ticker out from under us.
watch(() => props.activeTicker, () => { transcript.value = null })
</script>

<style scoped>
.step-workbench {
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
  font-size: 13px;
}

.empty-state {
  text-align: center;
  padding: 60px 20px;
}

.empty-state p {
  margin-bottom: 14px;
  color: #999;
}

.workbench-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 18px;
  border-bottom: 1px solid #EAEAEA;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.ticker-picker {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 700;
}

.ticker-picker select {
  font-family: inherit;
  padding: 4px 8px;
  border: 1px solid #CCC;
  border-radius: 3px;
  font-size: 12px;
}

.sub-tabs {
  display: flex;
  background: #F5F5F5;
  padding: 3px;
  border-radius: 5px;
  gap: 3px;
}

.tab-btn {
  border: none;
  background: transparent;
  padding: 5px 12px;
  font-size: 11px;
  font-weight: 600;
  color: #666;
  border-radius: 3px;
  cursor: pointer;
  font-family: inherit;
}

.tab-btn.active {
  background: #FFF;
  color: #000;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

.debate-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.debate-controls label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 700;
}

.num-input {
  width: 50px;
  font-family: inherit;
  padding: 4px 6px;
  border: 1px solid #CCC;
  border-radius: 3px;
}

.btn-primary {
  background: #000;
  color: #FFF;
  border: none;
  padding: 6px 14px;
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  border-radius: 3px;
}

.btn-primary:disabled {
  background: #CCC;
  cursor: not-allowed;
}

.btn-secondary {
  background: #FFF;
  color: #000;
  border: 1px solid #000;
  padding: 6px 14px;
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  border-radius: 3px;
}

.error-banner {
  margin: 10px 18px 0;
  background: #FFF0EF;
  border: 1px solid #D32F2F;
  color: #D32F2F;
  padding: 8px 12px;
  font-size: 11px;
  border-radius: 3px;
}

.loading-note {
  margin: 10px 18px 0;
  font-size: 11px;
  color: #999;
}

.workbench-body {
  flex: 1;
  min-height: 0;
}
</style>
