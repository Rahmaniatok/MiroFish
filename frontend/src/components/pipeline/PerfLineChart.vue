<template>
  <div class="plc" ref="root">
    <svg :width="width" :height="height" @mousemove="onMove" @mouseleave="hoverIdx = null" role="img" :aria-label="ariaLabel">
      <!-- grid + y ticks -->
      <g v-for="t in yTicks" :key="'y' + t">
        <line :x1="m.l" :x2="width - m.r" :y1="y(t)" :y2="y(t)" class="grid" :class="{ zero: t === 0 }" />
        <text :x="m.l - 8" :y="y(t)" class="tick" text-anchor="end" dominant-baseline="middle">{{ fmtY(t) }}</text>
      </g>
      <!-- x ticks -->
      <text v-for="t in xTicks" :key="'x' + t.i" :x="x(t.i)" :y="height - 6" class="tick" text-anchor="middle">{{ t.label }}</text>
      <!-- horizon markers -->
      <g v-for="h in horizonMarks" :key="h.label">
        <line :x1="x(h.i)" :x2="x(h.i)" :y1="m.t" :y2="height - m.b" class="horizon" />
        <text :x="x(h.i) + 4" :y="m.t + 10" class="horizon-label">{{ h.label }}</text>
      </g>
      <!-- series (dimmed ones first so the focused line sits on top) -->
      <path
        v-for="s in orderedSeries"
        :key="s.key"
        :d="path(s.values)"
        class="line"
        :stroke="s.color"
        :stroke-width="s.width || 2"
        :stroke-dasharray="s.dash || null"
        :opacity="focus && focus !== s.key ? 0.15 : 1"
      />
      <!-- direct end labels (only when few series: identity is never colour-alone) -->
      <g v-if="series.length <= 4">
        <text v-for="l in endLabels" :key="'l' + l.key" :x="width - m.r + 6" :y="l.y" class="end-label" dominant-baseline="middle">{{ l.text }}</text>
      </g>
      <!-- crosshair -->
      <g v-if="hoverIdx !== null">
        <line :x1="x(hoverIdx)" :x2="x(hoverIdx)" :y1="m.t" :y2="height - m.b" class="crosshair" />
        <circle v-for="s in series" :key="'c' + s.key" :cx="x(hoverIdx)" :cy="y(s.values[hoverIdx])" r="4" :fill="s.color" class="dot" />
      </g>
    </svg>
    <div v-if="hoverIdx !== null" class="tip" :style="tipStyle">
      <div class="tip-date">{{ dates[hoverIdx] }}</div>
      <div v-for="row in tipRows" :key="row.key" class="tip-row">
        <span class="sw" :style="{ background: row.color }"></span>
        <span class="tip-label">{{ row.label }}</span>
        <span class="tip-val">{{ fmtY(row.value, 2) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import * as d3 from 'd3'

const props = defineProps({
  dates: { type: Array, required: true },            // ['YYYY-MM-DD', ...]
  series: { type: Array, required: true },           // [{key,label,color,values,width?,dash?}]
  height: { type: Number, default: 360 },
  horizons: { type: Object, default: () => ({}) },   // {'4w': 'YYYY-MM-DD'}
  focus: { type: String, default: null },            // key of the emphasized series
  ariaLabel: { type: String, default: 'Line chart' }
})

const root = ref(null)
const width = ref(800)
const hoverIdx = ref(null)
const m = computed(() => ({ t: 14, r: props.series.length <= 4 ? 96 : 16, b: 26, l: 52 }))
let ro = null

const all = computed(() => props.series.flatMap(s => s.values).filter(v => v != null))
const yDomain = computed(() => {
  const lo = Math.min(0, d3.min(all.value) ?? 0)
  const hi = Math.max(0, d3.max(all.value) ?? 0)
  const pad = (hi - lo || 0.01) * 0.08
  return [lo - pad, hi + pad]
})
const x = (i) => m.value.l + (i / Math.max(1, props.dates.length - 1)) * (width.value - m.value.l - m.value.r)
const y = computed(() => d3.scaleLinear().domain(yDomain.value).range([props.height - m.value.b, m.value.t]))
const yTicks = computed(() => y.value.ticks(5))
const fmtY = (v, digits = 0) => (v > 0 ? '+' : '') + (v * 100).toFixed(digits) + '%'

const xTicks = computed(() => {
  const n = props.dates.length
  const count = Math.max(2, Math.floor(width.value / 110))
  const step = Math.max(1, Math.round(n / count))
  const out = []
  for (let i = 0; i < n; i += step) {
    const d = new Date(props.dates[i])
    out.push({ i, label: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) })
  }
  return out
})
const horizonMarks = computed(() => Object.entries(props.horizons || {})
  .map(([label, date]) => ({ label, i: props.dates.findIndex(d => d >= date) }))
  .filter(h => h.i > 0))

const line = computed(() => d3.line().defined(v => v != null).x((_, i) => x(i)).y(v => y.value(v)))
const path = (values) => line.value(values)
const orderedSeries = computed(() => [...props.series].sort((a, b) => (a.key === props.focus) - (b.key === props.focus)))

const endLabels = computed(() => {
  const labels = props.series.map(s => ({ key: s.key, text: s.label, y: y.value(s.values[s.values.length - 1]) }))
    .sort((a, b) => a.y - b.y)
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < 13) labels[i].y = labels[i - 1].y + 13   // nudge overlaps apart
  }
  return labels
})

const onMove = (e) => {
  const rect = e.currentTarget.getBoundingClientRect()
  const px = e.clientX - rect.left
  const frac = (px - m.value.l) / (width.value - m.value.l - m.value.r)
  hoverIdx.value = Math.max(0, Math.min(props.dates.length - 1, Math.round(frac * (props.dates.length - 1))))
}
const tipRows = computed(() => props.series
  .map(s => ({ key: s.key, label: s.label, color: s.color, value: s.values[hoverIdx.value] }))
  .filter(r => r.value != null)
  .sort((a, b) => b.value - a.value))
const tipStyle = computed(() => {
  const left = x(hoverIdx.value)
  const flip = left > width.value * 0.6
  return { left: flip ? `${left - 12}px` : `${left + 12}px`, transform: flip ? 'translateX(-100%)' : 'none' }
})

onMounted(() => {
  ro = new ResizeObserver(entries => { width.value = Math.max(320, entries[0].contentRect.width) })
  ro.observe(root.value)
})
onUnmounted(() => ro?.disconnect())
</script>

<style scoped>
.plc { position: relative; width: 100%; }
svg { display: block; overflow: visible; }
.grid { stroke: #ECEBE6; stroke-width: 1; }
.grid.zero { stroke: #C3C2B7; }
.tick { font-size: 10px; fill: #898781; font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace; }
.horizon { stroke: #FF5722; stroke-width: 1; stroke-dasharray: 3 3; opacity: 0.6; }
.horizon-label { font-size: 9.5px; font-weight: 800; fill: #FF5722; font-family: 'JetBrains Mono', monospace; }
.line { fill: none; stroke-linejoin: round; stroke-linecap: round; transition: opacity 0.2s; }
.end-label { font-size: 10.5px; font-weight: 600; fill: #52514E; }
.crosshair { stroke: #0B0B0B; stroke-width: 1; opacity: 0.35; }
.dot { stroke: #FFF; stroke-width: 2; }
.tip {
  position: absolute; top: 8px; pointer-events: none; z-index: 5; background: #FFF; border: 1px solid rgba(11,11,11,0.1);
  border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); padding: 8px 10px; min-width: 190px; font-size: 11.5px;
}
.tip-date { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; color: #898781; margin-bottom: 5px; }
.tip-row { display: flex; align-items: center; gap: 7px; line-height: 1.7; }
.sw { width: 10px; height: 3px; border-radius: 2px; flex-shrink: 0; }
.tip-label { color: #0B0B0B; flex: 1; white-space: nowrap; }
.tip-val { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; font-weight: 700; color: #0B0B0B; }
</style>
