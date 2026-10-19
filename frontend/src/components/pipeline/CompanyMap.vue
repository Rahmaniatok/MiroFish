<template>
  <div class="cmap" ref="wrapRef" :class="{ full }">
    <svg ref="svgRef" class="cmap-svg"></svg>

    <!-- top bar -->
    <div class="topbar">
      <span class="title">Company map</span>
      <div class="search">
        <input v-model="query" placeholder="Search ticker, company, sector…" @keydown.enter="focusSearch" />
        <span class="s-icon">⌕</span>
      </div>
      <div class="seg">
        <button :class="{ on: tieMode === 'mst' }" @click="tieMode = 'mst'" title="Each company linked to the peer it moves most like">Backbone</button>
        <button :class="{ on: tieMode === 'strong' }" @click="tieMode = 'strong'">+ Strong ties</button>
      </div>
      <label v-if="tieMode === 'strong'" class="rho">ρ ≥ {{ minRho.toFixed(2) }}
        <input type="range" min="0.4" max="0.95" step="0.05" v-model.number="minRho" />
      </label>
      <div class="seg">
        <button :class="{ on: colorBy === 'sector' }" @click="colorBy = 'sector'">Sector colour</button>
        <button :class="{ on: colorBy === 'cluster' }" @click="colorBy = 'cluster'">Cluster colour</button>
      </div>
      <button class="icon-btn" @click="fitView()" title="Fit everything in view">⊡</button>
      <button class="icon-btn" @click="relayout" title="Re-run the layout">↻</button>
      <button class="icon-btn" @click="full = !full" :title="full ? 'Exit full screen' : 'Full screen'">{{ full ? '⤡' : '⤢' }}</button>
    </div>

    <div v-if="loading" class="overlay"><span class="spin"></span> Building the map from a year of daily returns…</div>
    <div v-else-if="error" class="overlay err">{{ error }}</div>
    <div v-else-if="!data?.nodes?.length" class="overlay muted">No companies to map yet.</div>

    <!-- hover / pinned card -->
    <div v-if="card" class="card" :style="{ left: card.x + 'px', top: card.y + 'px' }">
      <div class="c-top">
        <b>{{ card.node.id }}</b><span class="c-name">{{ card.node.company_name }}</span>
      </div>
      <div class="c-row"><i :style="{ background: sectorColor(card.node.sector) }"></i>{{ card.node.sector }} · {{ card.node.industry }}</div>
      <div class="c-row" v-if="card.node.cluster != null"><i :style="{ background: clusterColor(card.node.cluster) }"></i>Cluster {{ card.node.cluster }} — {{ clusterById[card.node.cluster]?.name }}</div>
      <div class="c-row muted" v-else>Not enough price history for correlations</div>
      <div class="c-peers" v-if="card.node.top_peers?.length">
        Moves most like:
        <span v-for="p in card.node.top_peers" :key="p.ticker">{{ p.ticker }} <em>ρ {{ p.rho.toFixed(2) }}</em></span>
      </div>
      <div class="c-actions" v-if="pinned === card.node.id">
        <button v-if="removable" class="danger" @click="$emit('remove', card.node.id); pinned = ''">Remove from list</button>
        <button @click="pinned = ''">Close</button>
      </div>
    </div>

    <!-- legends -->
    <div class="legend sectors" v-if="data?.nodes?.length">
      <div class="lg-title" @click="showSectors = !showSectors">SECTOR <span>{{ showSectors ? '▾' : '▸' }}</span></div>
      <template v-if="showSectors">
        <div v-for="s in sectorsPresent" :key="s" class="lg-row"><i :style="{ background: sectorColor(s) }"></i>{{ s }}</div>
      </template>
    </div>
    <div class="legend clusters" v-if="data?.clusters?.length">
      <div class="lg-title" @click="showClusters = !showClusters">CLUSTERS <span>{{ showClusters ? '▾' : '▸' }}</span></div>
      <template v-if="showClusters">
        <div v-for="c in data.clusters" :key="c.id" class="lg-row click" :class="{ on: focusCluster === c.id, dim: focusCluster != null && focusCluster !== c.id }"
             @click="focusCluster = focusCluster === c.id ? null : c.id">
          <i :style="{ background: clusterColor(c.id) }"></i>
          <span>{{ c.id }} — {{ c.name }}</span>
          <em>{{ c.size }}<template v-if="c.avg_rho != null"> · avg ρ {{ c.avg_rho.toFixed(2) }}</template></em>
        </div>
      </template>
    </div>
    <div class="footnote" v-if="data?.window">
      Bridges = correlation of daily returns {{ data.window.start }} → {{ data.window.end }} ({{ data.window.days }} days, up to as_of).
      Backbone = minimum spanning tree; clusters = Louvain communities.
      <template v-if="data.no_history?.length"> No history: {{ data.no_history.join(', ') }}.</template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import * as d3 from 'd3'
import { sectorColor, clusterColor } from '../../utils/sectorColors'

const props = defineProps({
  data: { type: Object, default: null },      // company_map.build_map result
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  removable: { type: Boolean, default: false }
})
defineEmits(['remove'])

const wrapRef = ref(null)
const svgRef = ref(null)
const full = ref(false)
const query = ref('')
const tieMode = ref('mst')
const minRho = ref(0.6)
const colorBy = ref('sector')
const focusCluster = ref(null)
const showSectors = ref(true)
const showClusters = ref(true)
const narrow = () => (wrapRef.value?.clientWidth || 1000) < 900
const card = ref(null)
const pinned = ref('')

const clusterById = computed(() => Object.fromEntries((props.data?.clusters || []).map(c => [c.id, c])))
const sectorsPresent = computed(() => [...new Set((props.data?.nodes || []).map(n => n.sector))].sort())

let sim = null
let zoom = null
let nodesSel = null
let linksSel = null
let hullSel = null
let labelSel = null
let simNodes = []
let gRoot = null
let width = 800
let height = 600

const visibleEdges = () => (props.data?.edges || []).filter(e => e.mst || (tieMode.value === 'strong' && e.rho >= minRho.value))
const radius = (n) => n.market_cap ? Math.max(5, Math.min(14, Math.sqrt(n.market_cap / 1e9) * 0.9)) : 6
const fillOf = (n) => colorBy.value === 'sector' ? sectorColor(n.sector) : clusterColor(n.cluster)

const render = () => {
  if (!svgRef.value || !wrapRef.value) return
  sim?.stop()
  const svg = d3.select(svgRef.value)
  svg.selectAll('*').remove()
  card.value = null
  const data = props.data
  if (!data?.nodes?.length) return
  width = wrapRef.value.clientWidth
  height = wrapRef.value.clientHeight
  svg.attr('width', width).attr('height', height)

  gRoot = svg.append('g')
  zoom = d3.zoom().scaleExtent([0.2, 5]).on('zoom', (ev) => gRoot.attr('transform', ev.transform))
  svg.call(zoom).on('dblclick.zoom', null)
  svg.on('click', () => { pinned.value = ''; card.value = null })

  // cluster anchors on a circle, so communities settle into separate areas
  const clusters = data.clusters || []
  const R = Math.min(width, height) * (clusters.length > 1 ? 0.33 : 0)
  const anchor = {}
  clusters.forEach((c, i) => {
    const a = (i / Math.max(clusters.length, 1)) * 2 * Math.PI - Math.PI / 2
    anchor[c.id] = { x: width / 2 + R * Math.cos(a), y: height / 2 + R * Math.sin(a) }
  })
  const anchorOf = (n) => anchor[n.cluster] || { x: width / 2, y: height / 2 }

  const prev = Object.fromEntries(simNodes.map(n => [n.id, n]))
  simNodes = data.nodes.map(n => ({ ...n, r: radius(n), x: prev[n.id]?.x ?? anchorOf(n).x + (Math.random() - 0.5) * 60,
    y: prev[n.id]?.y ?? anchorOf(n).y + (Math.random() - 0.5) * 60 }))
  const byId = Object.fromEntries(simNodes.map(n => [n.id, n]))
  const links = visibleEdges().filter(e => byId[e.source] && byId[e.target])
    .map(e => ({ ...e, source: byId[e.source], target: byId[e.target] }))

  hullSel = gRoot.append('g').attr('class', 'hulls').selectAll('circle')
    .data(clusters).join('circle')
    .attr('fill', c => clusterColor(c.id)).attr('fill-opacity', 0.09)
    .attr('stroke', c => clusterColor(c.id)).attr('stroke-opacity', 0.35).attr('stroke-dasharray', '4 4')

  linksSel = gRoot.append('g').attr('class', 'links').selectAll('line')
    .data(links).join('line')
    .attr('stroke', e => e.mst ? '#94A3B8' : '#CBD5E1')
    .attr('stroke-width', e => e.mst ? 1.2 + Math.max(0, e.rho) * 1.3 : 0.8)
    .attr('stroke-opacity', e => e.mst ? 0.85 : 0.55)
    .attr('stroke-dasharray', e => e.mst ? null : '3 3')

  const nodeG = gRoot.append('g').attr('class', 'nodes').selectAll('g')
    .data(simNodes, d => d.id).join('g').attr('class', 'node').style('cursor', 'pointer')
  nodesSel = nodeG.append('circle').attr('r', d => d.r).attr('fill', fillOf)
    .attr('stroke', '#FFF').attr('stroke-width', 1.8)
  labelSel = nodeG.append('text').text(d => d.id).attr('x', d => d.r + 3).attr('y', 3.5)
    .attr('class', 'n-label')

  nodeG.on('mouseenter', (ev, d) => { if (!pinned.value) showCard(ev, d); highlight(d) })
    .on('mouseleave', () => { if (!pinned.value) card.value = null; highlight(null) })
    .on('click', (ev, d) => { ev.stopPropagation(); pinned.value = d.id; showCard(ev, d) })
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y })
      .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
      .on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null }))

  sim = d3.forceSimulation(simNodes)
    .force('link', d3.forceLink(links).distance(e => e.mst ? 40 + (1 - e.rho) * 60 : 90)
      .strength(e => e.mst ? 0.7 : 0.08 + Math.max(0, e.rho - 0.5) * 0.3))
    .force('charge', d3.forceManyBody().strength(-110))
    .force('collide', d3.forceCollide(d => d.r + 12))
    .force('x', d3.forceX(d => anchorOf(d).x).strength(0.07))
    .force('y', d3.forceY(d => anchorOf(d).y).strength(0.07))
    .on('tick', ticked)
  let fitted = false
  sim.on('tick.fit', () => { if (!fitted && sim.alpha() < 0.08) { fitted = true; fitView(false) } })
  applyFocus()
}

const ticked = () => {
  linksSel?.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y)
  d3.select(svgRef.value).selectAll('g.node').attr('transform', d => `translate(${d.x},${d.y})`)
  hullSel?.each(function (c) {
    const ms = simNodes.filter(n => n.cluster === c.id)
    if (!ms.length) return
    const cx = d3.mean(ms, n => n.x)
    const cy = d3.mean(ms, n => n.y)
    const r = d3.max(ms, n => Math.hypot(n.x - cx, n.y - cy) + n.r) + 28
    d3.select(this).attr('cx', cx).attr('cy', cy).attr('r', r)
  })
}

const showCard = (ev, d) => {
  const box = wrapRef.value.getBoundingClientRect()
  const x = Math.min(ev.clientX - box.left + 14, box.width - 290)
  const y = Math.min(ev.clientY - box.top + 14, box.height - 170)
  card.value = { node: d, x, y }
}

// neighbours of the hovered node stay bright, the rest fades
const highlight = (d) => {
  if (!nodesSel) return
  if (!d) { applyFocus(); return }
  const near = new Set([d.id])
  linksSel.each(e => { if (e.source.id === d.id) near.add(e.target.id); if (e.target.id === d.id) near.add(e.source.id) })
  d3.select(svgRef.value).selectAll('g.node').attr('opacity', n => near.has(n.id) ? 1 : 0.15)
  linksSel.attr('opacity', e => (e.source.id === d.id || e.target.id === d.id) ? 1 : 0.08)
}
const applyFocus = () => {
  if (!nodesSel) return
  const q = query.value.trim().toLowerCase()
  const match = (n) => !q || [n.id, n.company_name, n.sector, n.industry].some(x => (x || '').toLowerCase().includes(q))
  const inCluster = (n) => focusCluster.value == null || n.cluster === focusCluster.value
  d3.select(svgRef.value).selectAll('g.node').attr('opacity', n => match(n) && inCluster(n) ? 1 : 0.12)
  linksSel.attr('opacity', e => inCluster(e.source) && inCluster(e.target) && match(e.source) && match(e.target) ? 1 : 0.06)
  hullSel?.attr('fill-opacity', c => focusCluster.value == null || focusCluster.value === c.id ? 0.09 : 0.02)
}
const focusSearch = () => {
  const q = query.value.trim().toLowerCase()
  const n = simNodes.find(x => x.id.toLowerCase() === q) || simNodes.find(x => (x.company_name || '').toLowerCase().includes(q))
  if (!n || !zoom) return
  d3.select(svgRef.value).transition().duration(600)
    .call(zoom.transform, d3.zoomIdentity.translate(width / 2, height / 2).scale(2).translate(-n.x, -n.y))
}
// zoom so that every node fits, leaving room for the top bar and the legends
const fitView = (animate = true) => {
  if (!simNodes.length || !zoom) return
  const pad = 30
  const x0 = d3.min(simNodes, n => n.x - n.r) - pad
  const x1 = d3.max(simNodes, n => n.x + n.r + 40) + pad
  const y0 = d3.min(simNodes, n => n.y - n.r) - pad
  const y1 = d3.max(simNodes, n => n.y + n.r) + pad
  const top = 56
  const bottom = 40
  const k = Math.min(2, (width) / (x1 - x0), (height - top - bottom) / (y1 - y0))
  const t = d3.zoomIdentity.translate(width / 2, top + (height - top - bottom) / 2).scale(k)
    .translate(-(x0 + x1) / 2, -(y0 + y1) / 2)
  const sel = d3.select(svgRef.value)
  ;(animate ? sel.transition().duration(500) : sel).call(zoom.transform, t)
}
const relayout = () => { simNodes = []; render() }

watch(() => props.data, () => nextTick(render))
watch([tieMode, minRho], () => render())
watch(colorBy, () => nodesSel?.attr('fill', fillOf))
watch([query, focusCluster], applyFocus)
watch(full, () => nextTick(render))

let ro = null
let lastW = 0
onMounted(() => {
  if (narrow()) { showClusters.value = false; showSectors.value = false }
  render()
  ro = new ResizeObserver(() => {
    const w = wrapRef.value?.clientWidth || 0
    if (Math.abs(w - lastW) > 40) { lastW = w; render() }
  })
  ro.observe(wrapRef.value)
})
onUnmounted(() => { sim?.stop(); ro?.disconnect() })
</script>

<style scoped>
.cmap {
  position: relative; width: 100%; height: 100%; min-height: 520px; overflow: hidden;
  background: #FFF; background-image: radial-gradient(#E9ECF1 1px, transparent 1px); background-size: 18px 18px;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}
.cmap.full { position: fixed; inset: 0; z-index: 200; min-height: 0; }
.cmap-svg { display: block; width: 100%; height: 100%; }
.cmap :deep(.n-label) { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 700; fill: #334155; pointer-events: none; paint-order: stroke; stroke: #FFF; stroke-width: 3px; }

.topbar {
  position: absolute; top: 12px; left: 12px; right: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; z-index: 3;
  pointer-events: none;
}
.topbar > * { pointer-events: auto; }
.title { font-weight: 700; font-size: 13.5px; background: rgba(255,255,255,0.9); padding: 6px 10px; border-radius: 6px; }
.search { position: relative; }
.search input {
  width: 230px; border: 1px solid #E2E8F0; border-radius: 6px; padding: 7px 26px 7px 10px; font-size: 12px; outline: none;
  font-family: inherit; background: rgba(255,255,255,0.95);
}
.search input:focus { border-color: #000; }
.s-icon { position: absolute; right: 9px; top: 50%; transform: translateY(-50%); color: #94A3B8; }
.seg { display: inline-flex; background: rgba(241,245,249,0.95); border-radius: 6px; padding: 2px; }
.seg button { border: none; background: transparent; padding: 5px 9px; font-size: 11.5px; font-weight: 600; color: #64748B; cursor: pointer; border-radius: 4px; font-family: inherit; }
.seg button.on { background: #FFF; color: #000; box-shadow: 0 1px 2px rgba(0,0,0,0.12); }
.rho { display: inline-flex; align-items: center; gap: 6px; font-family: 'JetBrains Mono', monospace; font-size: 11px; background: rgba(255,255,255,0.95); padding: 4px 8px; border-radius: 6px; border: 1px solid #E2E8F0; }
.rho input { width: 90px; accent-color: #000; }
.icon-btn { border: 1px solid #E2E8F0; background: rgba(255,255,255,0.95); border-radius: 6px; width: 30px; height: 30px; cursor: pointer; font-size: 14px; }
.icon-btn:hover { border-color: #000; }

.overlay { position: absolute; inset: 0; display: grid; place-items: center; font-size: 13px; color: #475569; background: rgba(255,255,255,0.75); z-index: 2; }
.overlay.err { color: #B42318; }
.overlay.muted { color: #94A3B8; background: transparent; }
.spin { display: inline-block; width: 12px; height: 12px; border: 2px solid #CBD5E1; border-top-color: #000; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 8px; vertical-align: -2px; }
@keyframes spin { to { transform: rotate(360deg); } }

.card {
  position: absolute; z-index: 5; width: 275px; background: #FFF; border: 1px solid #E2E8F0; border-radius: 8px;
  box-shadow: 0 10px 28px rgba(15,23,42,0.16); padding: 10px 12px; font-size: 12px;
}
.c-top { display: flex; gap: 8px; align-items: baseline; margin-bottom: 6px; }
.c-top b { font-family: 'JetBrains Mono', monospace; font-size: 14px; }
.c-name { color: #475569; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-row { display: flex; gap: 6px; align-items: center; color: #334155; margin: 3px 0; }
.c-row i { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.c-row.muted { color: #94A3B8; }
.c-peers { margin-top: 6px; color: #64748B; font-size: 11px; display: flex; flex-wrap: wrap; gap: 4px 8px; }
.c-peers span { font-family: 'JetBrains Mono', monospace; color: #0F172A; font-weight: 700; }
.c-peers em { font-style: normal; color: #94A3B8; font-weight: 400; }
.c-actions { display: flex; gap: 6px; margin-top: 8px; }
.c-actions button { border: 1px solid #E2E8F0; background: #FFF; border-radius: 5px; padding: 4px 9px; font-size: 11px; cursor: pointer; font-family: inherit; }
.c-actions button.danger { border-color: #FCA5A5; color: #B91C1C; }
.c-actions button.danger:hover { background: #FEF2F2; }

.legend {
  position: absolute; bottom: 30px; z-index: 3; background: rgba(255,255,255,0.96); border: 1px solid #E2E8F0; border-radius: 8px;
  padding: 10px 12px; box-shadow: 0 4px 14px rgba(15,23,42,0.06); max-height: 46%; overflow-y: auto;
}
.legend.sectors { left: 12px; min-width: 170px; }
.legend.clusters { left: 200px; max-width: min(470px, calc(100% - 220px)); max-height: 38%; }
.lg-title { font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; color: #64748B; letter-spacing: 0.8px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
.lg-row { display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: #334155; padding: 2px 0; }
.lg-row i { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.lg-row.click { cursor: pointer; border-radius: 4px; padding: 3px 4px; }
.lg-row.click:hover { background: #F8FAFC; }
.lg-row.on { background: #F1F5F9; font-weight: 700; }
.lg-row.dim { opacity: 0.45; }
.lg-row span { flex: 1; min-width: 0; }
.lg-row em { font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #94A3B8; white-space: nowrap; }
.footnote { position: absolute; bottom: 8px; left: 12px; right: 12px; font-size: 10.5px; color: #94A3B8; z-index: 3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
