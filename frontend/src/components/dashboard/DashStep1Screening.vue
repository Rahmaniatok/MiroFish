<template>
  <div class="step-screening">
    <section class="filters">
      <div class="filter-group">
        <div class="filter-label">GICS Sector <span class="hint">(none = all)</span></div>
        <div class="checkbox-grid">
          <label v-for="sector in GICS_SECTORS" :key="sector" class="checkbox-item">
            <input type="checkbox" :value="sector" v-model="selectedSectors" />
            {{ sector }}
          </label>
        </div>
      </div>

      <div class="filter-row">
        <div class="filter-group">
          <div class="filter-label">Market Cap Tier <span class="hint">(none = all)</span></div>
          <div class="checkbox-grid tiers">
            <label v-for="tier in MARKET_CAP_TIERS" :key="tier" class="checkbox-item">
              <input type="checkbox" :value="tier" v-model="selectedTiers" />
              {{ tier }}
            </label>
          </div>
        </div>
        <div class="filter-group">
          <div class="filter-label">As-of Date <span class="hint">(optional)</span></div>
          <input type="date" v-model="asOfDate" class="date-input" />
        </div>
      </div>

      <button class="btn-primary" :disabled="loading" @click="runScreen">
        {{ loading ? 'Screening…' : 'Screen Universe' }}
      </button>
    </section>

    <div v-if="errorMessage" class="error-banner">
      <strong>Screening failed:</strong> {{ errorMessage }}
    </div>

    <section v-if="candidates.length" class="results">
      <div class="results-header">
        <span>{{ candidates.length }} candidates</span>
        <div class="select-controls">
          <button class="btn-link" @click="selectAll">Select all</button>
          <button class="btn-link" @click="clearSelection">Clear</button>
        </div>
      </div>

      <div class="table-scroll">
        <table class="candidates-table">
          <thead>
            <tr>
              <th></th>
              <th>Ticker</th>
              <th>Company</th>
              <th>Sector</th>
              <th>Cap</th>
              <th>Tier</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in candidates"
              :key="row.ticker"
              :class="{ active: row.ticker === lastFocused }"
              @click="focusTicker(row.ticker)"
            >
              <td @click.stop>
                <input type="checkbox" :value="row.ticker" v-model="selectedTickers" />
              </td>
              <td class="ticker-cell">{{ row.ticker }}</td>
              <td class="truncate">{{ row.company_name }}</td>
              <td class="truncate">{{ row.gics_sector }}</td>
              <td>{{ formatMarketCap(row.market_cap) }}</td>
              <td><span class="tier-badge" :class="row.market_cap_tier">{{ row.market_cap_tier }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-else-if="hasScreened && !loading" class="empty-state">
      No candidates matched these filters.
    </div>

    <footer class="continue-bar" v-if="candidates.length">
      <span>{{ selectedTickers.length }} selected</span>
      <button class="btn-primary" :disabled="!selectedTickers.length" @click="continueNext">
        Continue with {{ selectedTickers.length }} candidate{{ selectedTickers.length === 1 ? '' : 's' }} →
      </button>
    </footer>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { screenUniverse } from '../../api/universe'

const emit = defineEmits(['ticker-focus', 'add-log', 'continue'])

const GICS_SECTORS = [
  'Information Technology', 'Health Care', 'Financials',
  'Consumer Discretionary', 'Communication Services', 'Industrials',
  'Consumer Staples', 'Energy', 'Utilities', 'Real Estate', 'Materials'
]
const MARKET_CAP_TIERS = ['mega', 'large', 'mid', 'small']

const selectedSectors = ref([])
const selectedTiers = ref([])
const asOfDate = ref('')

const loading = ref(false)
const errorMessage = ref('')
const hasScreened = ref(false)
const candidates = ref([])
const selectedTickers = ref([])
const lastFocused = ref('')

function formatMarketCap(cap) {
  if (cap === null || cap === undefined) return '—'
  if (cap >= 1e12) return `$${(cap / 1e12).toFixed(2)}T`
  if (cap >= 1e9) return `$${(cap / 1e9).toFixed(1)}B`
  if (cap >= 1e6) return `$${(cap / 1e6).toFixed(1)}M`
  return `$${cap}`
}

async function runScreen() {
  loading.value = true
  errorMessage.value = ''
  emit('add-log', `Screening universe (sectors=${selectedSectors.value.join('|') || 'all'}, tiers=${selectedTiers.value.join('|') || 'all'})...`)
  try {
    const result = await screenUniverse({
      sectors: selectedSectors.value,
      marketCapTiers: selectedTiers.value,
      asOfDate: asOfDate.value || undefined
    })
    candidates.value = result
    selectedTickers.value = []
    hasScreened.value = true
    emit('add-log', `Screening complete: ${result.length} candidates.`)
  } catch (err) {
    errorMessage.value = err.message || 'Unknown error while screening the universe.'
    emit('add-log', `Screening error: ${errorMessage.value}`)
  } finally {
    loading.value = false
  }
}

function focusTicker(ticker) {
  lastFocused.value = ticker
  emit('ticker-focus', ticker)
}

function selectAll() {
  selectedTickers.value = candidates.value.map(r => r.ticker)
}

function clearSelection() {
  selectedTickers.value = []
}

function continueNext() {
  emit('continue', selectedTickers.value)
}
</script>

<style scoped>
.step-screening {
  height: 100%;
  overflow-y: auto;
  padding: 20px;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filters {
  border: 1px solid #EAEAEA;
  border-radius: 4px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.filter-row {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}

.filter-label {
  font-weight: 700;
  margin-bottom: 6px;
  font-size: 12px;
}

.hint {
  font-weight: 400;
  color: #999;
}

.checkbox-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px 12px;
}

.checkbox-grid.tiers {
  grid-template-columns: repeat(4, auto);
}

.checkbox-item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  cursor: pointer;
}

.date-input {
  font-family: inherit;
  padding: 5px 8px;
  border: 1px solid #CCC;
  border-radius: 3px;
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
  align-self: flex-start;
}

.btn-primary:disabled {
  background: #CCC;
  cursor: not-allowed;
}

.error-banner {
  background: #FFF0EF;
  border: 1px solid #D32F2F;
  color: #D32F2F;
  padding: 10px 14px;
  font-size: 12px;
  border-radius: 3px;
}

.results-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
}

.select-controls {
  display: flex;
  gap: 10px;
}

.btn-link {
  background: none;
  border: none;
  text-decoration: underline;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  padding: 0;
}

.table-scroll {
  overflow-x: auto;
  border: 1px solid #EAEAEA;
  border-radius: 4px;
}

.candidates-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.candidates-table th,
.candidates-table td {
  border-bottom: 1px solid #F0F0F0;
  padding: 7px 8px;
  text-align: left;
  white-space: nowrap;
}

.candidates-table tbody tr {
  cursor: pointer;
}

.candidates-table tbody tr:hover {
  background: #FAFAFA;
}

.candidates-table tbody tr.active {
  background: #F0F0F0;
}

.truncate {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ticker-cell {
  font-weight: 700;
}

.tier-badge {
  padding: 1px 6px;
  border: 1px solid #000;
  font-size: 10px;
  text-transform: uppercase;
  border-radius: 2px;
}

.empty-state {
  color: #999;
  padding: 30px 0;
  text-align: center;
}

.continue-bar {
  margin-top: auto;
  border-top: 1px solid #EAEAEA;
  padding-top: 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
