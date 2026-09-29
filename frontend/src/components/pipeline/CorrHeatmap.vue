<template>
  <div class="hm">
    <div v-if="!tickers.length" class="hm-empty">Needs at least two tickers.</div>
    <div v-else class="hm-grid" :style="{ gridTemplateColumns: `44px repeat(${tickers.length}, minmax(0, 52px))` }">
      <span></span>
      <span v-for="t in tickers" :key="'h' + t" class="hm-h">{{ t }}</span>
      <template v-for="(a, i) in tickers" :key="'r' + a">
        <span class="hm-h row">{{ a }}</span>
        <span
          v-for="(b, j) in tickers"
          :key="a + b"
          class="hm-cell"
          :style="{ background: color(values[i][j]) }"
          @mouseenter="hover = { a, b, v: values[i][j] }"
          @mouseleave="hover = null"
        >
          <em v-if="tickers.length <= 12 && values[i][j] != null" :class="{ light: Math.abs(norm(values[i][j])) > 0.55 }">{{ fmt(values[i][j], true) }}</em>
        </span>
      </template>
    </div>
    <div class="hm-foot">
      <div class="scale">
        <span>{{ mode === 'corr' ? '−1' : fmt(-maxAbs) }}</span>
        <span class="ramp"></span>
        <span>{{ mode === 'corr' ? '+1' : fmt(maxAbs) }}</span>
      </div>
      <span v-if="hover" class="hm-tip"><b>{{ hover.a }}</b> × <b>{{ hover.b }}</b> = {{ fmt(hover.v) }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import * as d3 from 'd3'

const props = defineProps({
  tickers: { type: Array, required: true },
  values: { type: Array, required: true },   // square matrix
  mode: { type: String, default: 'corr' }     // 'corr' | 'cov'
})
const hover = ref(null)

// diverging: blue (negative) — neutral gray — red (positive)
const NEG = '#2a78d6'
const MID = '#f0efec'
const POS = '#e34948'
const maxAbs = computed(() => props.mode === 'corr' ? 1
  : Math.max(1e-9, ...props.values.flat().filter(v => v != null).map(Math.abs)))
const norm = (v) => v == null ? 0 : Math.max(-1, Math.min(1, v / maxAbs.value))
const color = (v) => {
  if (v == null) return '#FAFAFA'
  const t = norm(v)
  return t >= 0 ? d3.interpolateLab(MID, POS)(t) : d3.interpolateLab(MID, NEG)(-t)
}
const fmt = (v, short = false) => {
  if (v == null) return '—'
  if (props.mode === 'corr') return short ? v.toFixed(2).replace('0.', '.').replace('-.', '−.') : v.toFixed(3)
  // annualized covariance of daily returns, plain decimal (variance on the diagonal)
  return short ? v.toFixed(3).replace('0.', '.').replace('-.', '−.') : v.toFixed(4)
}
</script>

<style scoped>
.hm-empty { font-size: 12px; color: #AAA; padding: 20px 0; }
.hm-grid { display: grid; gap: 2px; }
.hm-h { font-family: 'JetBrains Mono', monospace; font-size: 9px; font-weight: 800; color: #52514E; text-align: center; align-self: end; overflow: hidden; }
.hm-h.row { text-align: right; padding-right: 4px; align-self: center; }
.hm-cell { aspect-ratio: 1; border-radius: 3px; display: grid; place-items: center; cursor: crosshair; transition: transform 0.1s; }
.hm-cell:hover { transform: scale(1.12); box-shadow: 0 0 0 2px #0B0B0B; z-index: 1; }
.hm-cell em { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 8.5px; color: #0B0B0B; font-variant-numeric: tabular-nums; }
.hm-cell em.light { color: #FFF; }
.hm-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; gap: 10px; min-height: 18px; }
.scale { display: flex; align-items: center; gap: 6px; font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: #898781; }
.ramp { width: 120px; height: 8px; border-radius: 4px; background: linear-gradient(90deg, #2a78d6, #f0efec, #e34948); }
.hm-tip { font-size: 11px; color: #0B0B0B; font-family: 'JetBrains Mono', monospace; }
</style>
