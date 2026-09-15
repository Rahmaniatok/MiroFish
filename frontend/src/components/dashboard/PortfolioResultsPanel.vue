<template>
  <section class="portfolio-results">
    <div class="metrics-row">
      <div class="metric-card">
        <div class="metric-label">Expected Return</div>
        <div class="metric-value">{{ pct(portfolio.portfolio.expected_return, true) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Volatility</div>
        <div class="metric-value">{{ pct(portfolio.portfolio.volatility) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Sharpe</div>
        <div class="metric-value">{{ portfolio.portfolio.sharpe_ratio.toFixed(3) }}</div>
      </div>
      <div class="metric-card" v-if="portfolio.portfolio.model">
        <div class="metric-label">Model</div>
        <div class="metric-value model-value">{{ MODEL_LABELS[portfolio.portfolio.model] || portfolio.portfolio.model }}</div>
      </div>
    </div>

    <section class="block">
      <h4>Holdings ({{ holdings.length }})</h4>
      <table class="data-table">
        <thead>
          <tr><th></th><th>Ticker</th><th>Weight</th><th>Consensus</th><th>Rank</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in holdings" :key="h.ticker" @click="$emit('ticker-focus', h.ticker)" class="clickable">
            <td class="bar-cell"><div class="bar" :style="{ width: (h.weight * 100) + '%' }"></div></td>
            <td class="ticker-cell">{{ h.ticker }}</td>
            <td>{{ pct(h.weight) }}</td>
            <td>{{ h.consensus_score.toFixed(3) }}</td>
            <td>#{{ h.consensus_rank }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="block" v-if="zeroWeight.length">
      <h4>Zero Weight / Dropped ({{ zeroWeight.length }})</h4>
      <table class="data-table">
        <thead><tr><th>Ticker</th><th>Reason</th></tr></thead>
        <tbody>
          <tr v-for="h in zeroWeight" :key="h.ticker" @click="$emit('ticker-focus', h.ticker)" class="clickable">
            <td class="ticker-cell">{{ h.ticker }}</td>
            <td>{{ h.dropped_reason || 'Optimizer assigned ~0% (inefficient trade-off)' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="block compliance-block" v-if="excludedNonCompliant.length">
      <h4>⛔ Excluded — Sharia Hard Filter ({{ excludedNonCompliant.length }})</h4>
      <div class="compliance-card" v-for="e in excludedNonCompliant" :key="e.ticker">
        <div class="compliance-header">
          <span class="ticker-cell">{{ e.ticker }}</span>
          <span class="verdict-badge">{{ e.compliance_verdict }}</span>
        </div>
        <div class="compliance-reason">"{{ e.compliance_reason }}"</div>
      </div>
    </section>

    <section class="block" v-if="skipped.length">
      <h4>Skipped ({{ skipped.length }})</h4>
      <ul class="skipped-list">
        <li v-for="s in skipped" :key="s.ticker">{{ s.ticker }}: {{ s.reason }}</li>
      </ul>
    </section>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  portfolio: { type: Object, required: true }
})
defineEmits(['ticker-focus'])

const MODEL_LABELS = {
  max_sharpe: 'Max Sharpe',
  min_variance: 'Min Variance',
  hrp: 'HRP'
}

const holdings = computed(() => (props.portfolio.holdings || []).filter(h => h.weight > 1e-9))
const zeroWeight = computed(() => (props.portfolio.holdings || []).filter(h => h.weight <= 1e-9))
const excludedNonCompliant = computed(() => props.portfolio.screening?.excluded_non_compliant || [])
const skipped = computed(() => props.portfolio.screening?.skipped || [])

function pct(x, signed = false) {
  if (x === null || x === undefined) return '—'
  const v = (x * 100).toFixed(2)
  return signed && x >= 0 ? `+${v}%` : `${v}%`
}
</script>

<style scoped>
.metrics-row {
  display: flex;
  gap: 10px;
  margin-bottom: 20px;
}

.metric-card {
  flex: 1;
  border: 1px solid #EAEAEA;
  border-radius: 4px;
  padding: 10px;
  text-align: center;
}

.metric-label {
  font-size: 10px;
  color: #999;
  margin-bottom: 4px;
}

.metric-value {
  font-size: 16px;
  font-weight: 700;
}

.metric-value.model-value {
  font-size: 13px;
}

.block {
  margin-bottom: 22px;
}

.block h4 {
  font-size: 13px;
  margin-bottom: 8px;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.data-table th,
.data-table td {
  border-bottom: 1px solid #F0F0F0;
  padding: 6px 8px;
  text-align: left;
}

.data-table tbody tr.clickable {
  cursor: pointer;
}

.data-table tbody tr.clickable:hover {
  background: #FAFAFA;
}

.ticker-cell {
  font-weight: 700;
}

.bar-cell {
  width: 25%;
}

.bar {
  height: 8px;
  background: #000;
}

.compliance-block {
  border: 2px solid #D32F2F;
  border-radius: 4px;
  padding: 14px;
  background: #FFF8F8;
}

.compliance-card {
  border: 1px solid #D32F2F;
  border-radius: 3px;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: #FFF;
}

.compliance-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 5px;
}

.verdict-badge {
  background: #D32F2F;
  color: #FFF;
  padding: 1px 6px;
  font-size: 10px;
  text-transform: uppercase;
  border-radius: 2px;
}

.compliance-reason {
  font-size: 12px;
  font-style: italic;
}

.skipped-list {
  font-size: 12px;
  color: #999;
  padding-left: 18px;
}
</style>
