<template>
  <div class="step-build">
    <div v-if="!candidateTickers.length" class="empty-state">
      <p>No candidate tickers loaded yet.</p>
      <button class="btn-primary" @click="$emit('go-back')">← Back to Screening</button>
    </div>

    <template v-else>
      <section class="build-form">
        <div class="candidates-summary">
          <strong>{{ candidateTickers.length }}</strong> candidates:
          <span class="ticker-list">{{ candidateTickers.join(', ') }}</span>
        </div>

        <div class="form-row">
          <label>
            Top K
            <input type="number" min="1" v-model.number="topK" class="num-input" />
          </label>
          <label>
            Max Weight (%)
            <input type="number" min="1" max="100" v-model.number="maxWeightPct" class="num-input" />
          </label>
          <label>
            Model
            <select v-model="model" class="model-select">
              <option value="max_sharpe">Max Sharpe</option>
              <option value="min_variance">Min Variance</option>
              <option value="hrp">HRP (Risk Parity)</option>
            </select>
          </label>
          <label>
            As-of Date <span class="hint">(optional)</span>
            <input type="date" v-model="asOfDate" class="date-input" />
          </label>
        </div>
        <p class="model-hint">{{ MODEL_HINTS[model] }}</p>

        <div class="button-row">
          <button class="btn-secondary" @click="$emit('go-back')">← Back</button>
          <button class="btn-primary" :disabled="loading" @click="runBuild">
            {{ loading ? 'Building…' : 'Build Portfolio' }}
          </button>
        </div>
      </section>

      <div v-if="errorMessage" class="error-banner">
        <strong>Build failed:</strong> {{ errorMessage }}
      </div>

      <PortfolioResultsPanel
        v-if="portfolio"
        :portfolio="portfolio"
        @ticker-focus="$emit('ticker-focus', $event)"
      />

      <div v-if="portfolio" class="view-final-link">
        <router-link to="/dashboard/portfolio">View as a standalone page →</router-link>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { buildPortfolio } from '../../api/portfolio'
import PortfolioResultsPanel from './PortfolioResultsPanel.vue'

const props = defineProps({
  candidateTickers: { type: Array, default: () => [] }
})
const emit = defineEmits(['go-back', 'ticker-focus', 'add-log', 'built'])

const topK = ref(25)
const maxWeightPct = ref(20)
const model = ref('max_sharpe')
const asOfDate = ref('')

const MODEL_HINTS = {
  max_sharpe: 'Maximizes risk-adjusted return (max-Sharpe mean-variance).',
  min_variance: 'Minimizes portfolio volatility, ignoring expected return.',
  hrp: 'Hierarchical Risk Parity — clusters correlated assets and allocates by risk, not by an expected-return forecast.'
}

const loading = ref(false)
const errorMessage = ref('')
const portfolio = ref(null)

async function runBuild() {
  loading.value = true
  errorMessage.value = ''
  emit('add-log', `Building portfolio (top_k=${topK.value}, max_weight=${maxWeightPct.value}%, model=${model.value})...`)
  try {
    const result = await buildPortfolio({
      candidateTickers: props.candidateTickers,
      asOfDate: asOfDate.value || null,
      topK: topK.value,
      maxWeight: maxWeightPct.value / 100,
      model: model.value
    })
    portfolio.value = result
    emit('add-log', `Portfolio built: ${Object.keys(result.weights).length} holdings, Sharpe ${result.portfolio.sharpe_ratio.toFixed(3)}.`)
    emit('built', result)
  } catch (err) {
    errorMessage.value = err.message || 'Unknown error while building the portfolio.'
    emit('add-log', `Build error: ${errorMessage.value}`)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.step-build {
  height: 100%;
  overflow-y: auto;
  padding: 20px;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
  font-size: 13px;
}

.empty-state {
  text-align: center;
  padding: 60px 0;
}

.empty-state p {
  margin-bottom: 14px;
  color: #999;
}

.build-form {
  border: 1px solid #EAEAEA;
  border-radius: 4px;
  padding: 16px;
  margin-bottom: 16px;
}

.candidates-summary {
  font-size: 12px;
  margin-bottom: 14px;
}

.ticker-list {
  color: #666;
}

.form-row {
  display: flex;
  gap: 20px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.form-row label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  font-weight: 700;
}

.hint {
  font-weight: 400;
  color: #999;
}

.num-input,
.date-input,
.model-select {
  font-family: inherit;
  padding: 5px 8px;
  border: 1px solid #CCC;
  border-radius: 3px;
  width: 120px;
}

.model-select {
  width: 160px;
}

.model-hint {
  font-size: 11px;
  color: #999;
  margin-top: -6px;
  margin-bottom: 14px;
}

.button-row {
  display: flex;
  gap: 10px;
}

.btn-primary {
  background: #000;
  color: #FFF;
  border: none;
  padding: 9px 18px;
  font-family: inherit;
  font-size: 13px;
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
  padding: 9px 18px;
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  border-radius: 3px;
}

.error-banner {
  background: #FFF0EF;
  border: 1px solid #D32F2F;
  color: #D32F2F;
  padding: 10px 14px;
  font-size: 12px;
  border-radius: 3px;
  margin-bottom: 16px;
  white-space: pre-wrap;
}

.view-final-link {
  margin-top: 16px;
  text-align: right;
}

.view-final-link a {
  font-size: 12px;
  color: #000;
  text-decoration: underline;
}
</style>
