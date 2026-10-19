<template>
  <div class="nl" :style="{ width: width + 'px' }">
    <svg :width="width" :height="height" class="nl-svg" @mousedown="startBrush" ref="svgRef">
      <!-- track + gutters for values outside the group's (outlier-free) min..max -->
      <rect :x="G" y="0" :width="width - 2 * G" :height="height" class="track" />
      <text v-if="leftOut.length" :x="G / 2" :y="AXIS - 18" class="gutter-label" text-anchor="middle">&lt; min</text>
      <text v-if="rightOut.length" :x="width - G / 2" :y="AXIS - 18" class="gutter-label" text-anchor="middle">&gt; max</text>
      <line :x1="G" :x2="width - G" :y1="AXIS" :y2="AXIS" class="axis" />

      <!-- the four fixed markers -->
      <g v-for="mk in markers" :key="mk.key">
        <line :x1="mk.x" :x2="mk.x" :y1="AXIS - 22" :y2="height - 4" :stroke="mk.color" :stroke-width="mk.key === 'median' || mk.key === 'mean' ? 2 : 1.6"
              :stroke-dasharray="mk.key === 'mean' ? '5 3' : null" />
        <text :x="mk.x + (mk.anchor === 'start' ? 4 : mk.anchor === 'end' ? -4 : 0)" :y="mk.tier === 0 ? 11 : 24"
              :text-anchor="mk.anchor" class="mk-label" :fill="mk.color">{{ mk.text }}</text>
      </g>

      <!-- brush -->
      <rect v-if="brush" :x="Math.min(brush.x0, brush.x1)" y="0" :width="Math.abs(brush.x1 - brush.x0)" :height="height"
            class="brush" />

      <!-- tickers -->
      <g v-for="it in placed" :key="it.ticker" class="tk" :class="{ removed: it.removed, hit: brushSet.has(it.ticker), focus: focus === it.ticker }"
         @mousedown.stop @click.stop="$emit('toggle', it.ticker)" @mouseenter="$emit('hover', it.ticker)" @mouseleave="$emit('hover', '')">
        <line :x1="it.x" :x2="it.x" :y1="AXIS" :y2="it.y - 9" class="leader" />
        <circle :cx="it.x" :cy="AXIS" r="3.2" class="dot" />
        <rect :x="it.left - 3" :y="it.y - 9" :width="it.w + 6" height="15" rx="3" class="chip" />
        <text :x="it.left" :y="it.y + 2" class="tk-text"><tspan class="tk-sym">{{ it.ticker }}</tspan> ({{ it.label }})</text>
        <title>{{ it.ticker }} · {{ it.label }}{{ it.removed ? ' — removed (click to keep)' : ' — click to remove' }}</title>
      </g>
    </svg>
    <div v-if="brush && brush.done && brushList.length" class="brush-bar" :style="{ left: Math.min(Math.max(brush.x1, brush.x0), width - 260) + 'px' }">
      <b>{{ brushList.length }}</b> in range
      <button @click="emitBrush('remove')">Remove</button>
      <button @click="emitBrush('keep')">Keep only these</button>
      <button class="x" @click="brush = null">✕</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted } from 'vue'

// One "number line": a group's min / max / median / average as fixed-colour lines and every
// company as "TICKER (value)", stacked into rows so labels never overlap.
const props = defineProps({
  stats: { type: Object, required: true },      // {min, max, median, mean, ...}
  items: { type: Array, required: true },       // [{ticker, value, removed}]
  width: { type: Number, default: 900 },
  fmt: { type: Function, required: true },
  focus: { type: String, default: '' }
})
const emit = defineEmits(['toggle', 'hover', 'brush'])

const G = 46                 // gutter for values beyond min / max
const AXIS = 44
const ROW = 19
const CHAR = 6.4             // JetBrains Mono 10.5px advance
const COLORS = { min: '#E53935', max: '#111111', median: '#1E63E9', mean: '#16A34A' }

const lo = computed(() => props.stats.min)
const hi = computed(() => props.stats.max === props.stats.min ? props.stats.max + (Math.abs(props.stats.max) * 0.1 || 1) : props.stats.max)
const inner = computed(() => props.width - 2 * G - 24)
const sx = (v) => G + 12 + (v - lo.value) / (hi.value - lo.value) * inner.value

const markers = computed(() => {
  const s = props.stats
  const out = [
    { key: 'min', x: sx(s.min), text: `MIN ${props.fmt(s.min)}`, tier: 0, anchor: 'start' },
    { key: 'max', x: sx(s.max), text: `MAX ${props.fmt(s.max)}`, tier: 0, anchor: 'end' },
    { key: 'median', x: sx(s.median), text: `MEDIAN ${props.fmt(s.median)}`, tier: 1 },
    { key: 'mean', x: sx(s.mean), text: `AVG ${props.fmt(s.mean)}`, tier: 1 }
  ]
  // median and average labels point away from each other
  const medLeft = s.median <= s.mean
  out[2].anchor = medLeft ? 'end' : 'start'
  out[3].anchor = medLeft ? 'start' : 'end'
  out.forEach(m => { m.color = COLORS[m.key] })
  return out
})

const leftOut = computed(() => props.items.filter(i => i.value < props.stats.min))
const rightOut = computed(() => props.items.filter(i => i.value > props.stats.max))

// greedy row packing: sort by x, put each label in the first row where it fits
const placed = computed(() => {
  const rows = []
  const list = props.items.map(it => {
    let x
    if (it.value < props.stats.min) x = G / 2
    else if (it.value > props.stats.max) x = props.width - G / 2
    else x = sx(it.value)
    const label = props.fmt(it.value)
    const w = (it.ticker.length + label.length + 3) * CHAR
    return { ...it, x, label, w }
  }).sort((a, b) => a.x - b.x)
  list.forEach(it => {
    it.left = Math.min(Math.max(it.x - it.w / 2, 4), props.width - it.w - 4)
    let r = rows.findIndex(end => end + 8 <= it.left)
    if (r === -1) { rows.push(0); r = rows.length - 1 }
    rows[r] = it.left + it.w + 6
    it.y = AXIS + 22 + r * ROW
  })
  return list
})
const height = computed(() => Math.max(AXIS + 30, ...placed.value.map(p => p.y + 12)))

// ---- brush: drag on the line to pick a value range ----
const svgRef = ref(null)
const brush = ref(null)
const toX = (e) => e.clientX - svgRef.value.getBoundingClientRect().left
const startBrush = (e) => {
  if (e.button !== 0) return
  brush.value = { x0: toX(e), x1: toX(e), done: false }
  window.addEventListener('mousemove', moveBrush)
  window.addEventListener('mouseup', endBrush)
}
const moveBrush = (e) => { if (brush.value) brush.value.x1 = toX(e) }
const endBrush = () => {
  window.removeEventListener('mousemove', moveBrush)
  window.removeEventListener('mouseup', endBrush)
  if (!brush.value) return
  if (Math.abs(brush.value.x1 - brush.value.x0) < 6) brush.value = null
  else brush.value.done = true
}
onUnmounted(endBrush)
const brushList = computed(() => {
  if (!brush.value) return []
  const a = Math.min(brush.value.x0, brush.value.x1)
  const b = Math.max(brush.value.x0, brush.value.x1)
  return placed.value.filter(p => p.x >= a && p.x <= b).map(p => p.ticker)
})
const brushSet = computed(() => new Set(brushList.value))
const emitBrush = (action) => {
  emit('brush', { action, tickers: brushList.value })
  brush.value = null
}
</script>

<style scoped>
.nl { position: relative; user-select: none; }
.nl-svg { display: block; cursor: crosshair; }
.track { fill: #FBFBFB; }
.axis { stroke: #CFCFCF; stroke-width: 1.5; }
.gutter-label { font-family: 'JetBrains Mono', monospace; font-size: 9px; fill: #AAA; }
.mk-label { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; }
.brush { fill: rgba(30, 99, 233, 0.08); stroke: rgba(30, 99, 233, 0.5); stroke-dasharray: 4 3; }
.tk { cursor: pointer; }
.tk .leader { stroke: #CBD5E1; stroke-width: 1; }
.tk .dot { fill: #334155; }
.tk .chip { fill: #FFF; stroke: #E2E8F0; }
.tk-text { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; fill: #64748B; }
.tk-sym { font-weight: 800; fill: #334155; }
.tk:hover .chip, .tk.focus .chip { stroke: #334155; stroke-width: 1.5; }
.tk:hover .leader, .tk.focus .leader { stroke: #334155; }
.tk.hit .chip { fill: #EAF1FF; stroke: #1E63E9; }
.tk.removed .dot { fill: #D5D9DF; }
.tk.removed .chip { fill: #F7F7F7; stroke: #EEE; }
.tk.removed .tk-text, .tk.removed .tk-sym { fill: #C2C7CE; text-decoration: line-through; }
.tk.removed .leader { stroke: #EEE; }
.brush-bar {
  position: absolute; top: 4px; display: flex; align-items: center; gap: 6px; background: #111; color: #FFF;
  padding: 6px 8px; border-radius: 6px; font-size: 11.5px; box-shadow: 0 6px 18px rgba(0,0,0,0.2); z-index: 3; white-space: nowrap;
}
.brush-bar button { border: none; border-radius: 4px; padding: 4px 8px; font-size: 11px; font-weight: 700; cursor: pointer; background: #FFF; color: #111; font-family: inherit; }
.brush-bar button.x { background: transparent; color: #AAA; }
</style>
