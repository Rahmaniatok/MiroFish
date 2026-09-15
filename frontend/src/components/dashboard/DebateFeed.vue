<template>
  <div class="debate-feed">
    <div v-if="transcript" class="gamma-verdict-banner">
      <span class="gamma-tag">GAMMA VERDICT</span>
      <strong>{{ transcript.gamma_verdict.verdict }}</strong>
      — {{ transcript.gamma_verdict.rationale }}
    </div>

    <div class="timeline-feed">
      <div class="timeline-axis"></div>

      <TransitionGroup name="timeline-item">
        <div
          v-for="(item, i) in visibleItems"
          :key="`${item.roundNum}-${item.statement.persona_key}`"
          class="timeline-item"
        >
          <div v-if="item.isFirstOfRound" class="round-divider">Round {{ item.roundNum }}</div>

          <div class="timeline-marker"><div class="marker-dot" :class="{ compliance: item.statement.role === 'compliance' }"></div></div>

          <div class="timeline-card" :class="{ compliance: item.statement.role === 'compliance' }">
            <div class="card-header">
              <div class="agent-info">
                <div class="avatar-placeholder" :class="{ compliance: item.statement.role === 'compliance' }">
                  {{ (item.statement.persona_name || 'A')[0] }}
                </div>
                <span class="agent-name">{{ item.statement.persona_name }}</span>
              </div>
              <div class="header-meta">
                <div class="role-badge" :class="{ compliance: item.statement.role === 'compliance' }">
                  {{ item.statement.role === 'compliance' ? 'COMPLIANCE' : 'INVESTOR' }}
                </div>
                <div class="disposition-badge">{{ item.statement.disposition }}</div>
              </div>
            </div>
            <div class="card-body">{{ item.statement.text }}</div>
            <div class="card-footer">
              <span class="time-tag">R{{ item.roundNum }}</span>
            </div>
          </div>
        </div>
      </TransitionGroup>

      <div v-if="!transcript" class="waiting-state">
        <span>No debate run yet.</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onUnmounted } from 'vue'

const props = defineProps({
  transcript: { type: Object, default: null }
})

const visibleItems = ref([])
let revealTimer = null
let queue = []

function buildQueue(transcript) {
  const items = []
  if (!transcript) return items
  for (let r = 1; r <= transcript.rounds; r++) {
    const statements = transcript.statements[r - 1] || []
    statements.forEach((statement, idx) => {
      items.push({ roundNum: r, statement, isFirstOfRound: idx === 0 })
    })
  }
  return items
}

function startReveal() {
  clearInterval(revealTimer)
  visibleItems.value = []
  queue = buildQueue(props.transcript)
  if (!queue.length) return

  let i = 0
  const revealNext = () => {
    if (i >= queue.length) {
      clearInterval(revealTimer)
      return
    }
    visibleItems.value.push(queue[i])
    i++
  }
  revealNext()
  revealTimer = setInterval(revealNext, 400)
}

watch(() => props.transcript, () => startReveal(), { immediate: true })
onUnmounted(() => clearInterval(revealTimer))
</script>

<style scoped>
.debate-feed {
  height: 100%;
  overflow-y: auto;
  padding: 20px;
  font-family: 'JetBrains Mono', 'Space Grotesk', monospace;
}

.gamma-verdict-banner {
  border: 2px solid #7B1FA2;
  background: #F8F0FB;
  border-radius: 4px;
  padding: 12px 16px;
  margin-bottom: 20px;
  font-size: 12px;
  max-width: 900px;
  margin-left: auto;
  margin-right: auto;
}

.gamma-tag {
  display: inline-block;
  background: #7B1FA2;
  color: #FFF;
  padding: 2px 8px;
  font-size: 10px;
  margin-right: 8px;
  border-radius: 2px;
}

.timeline-feed {
  position: relative;
  max-width: 700px;
  margin: 0 auto;
}

.timeline-axis {
  position: absolute;
  left: 15px;
  top: 0;
  bottom: 0;
  width: 1px;
  background: #EAEAEA;
}

.timeline-item {
  position: relative;
  padding-left: 40px;
  margin-bottom: 18px;
}

.round-divider {
  font-size: 11px;
  font-weight: 700;
  color: #999;
  margin-bottom: 10px;
  padding-top: 6px;
}

.timeline-marker {
  position: absolute;
  left: 11px;
  top: 6px;
  width: 10px;
  height: 10px;
  background: #FFF;
  border: 1px solid #CCC;
  border-radius: 50%;
}

.marker-dot {
  width: 4px;
  height: 4px;
  margin: 2px;
  background: #000;
  border-radius: 50%;
}

.marker-dot.compliance {
  background: #7B1FA2;
}

.timeline-card {
  border: 1px solid #EAEAEA;
  border-radius: 3px;
  padding: 12px 14px;
  background: #FFF;
}

.timeline-card.compliance {
  border: 2px solid #7B1FA2;
  background: #FBF8FD;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid #F5F5F5;
}

.agent-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.avatar-placeholder {
  width: 22px;
  height: 22px;
  background: #000;
  color: #FFF;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
}

.avatar-placeholder.compliance {
  background: #7B1FA2;
}

.agent-name {
  font-size: 12px;
  font-weight: 700;
}

.header-meta {
  display: flex;
  gap: 6px;
  align-items: center;
}

.role-badge {
  background: #000;
  color: #FFF;
  padding: 1px 6px;
  font-size: 9px;
  border-radius: 2px;
}

.role-badge.compliance {
  background: #7B1FA2;
}

.disposition-badge {
  color: #999;
  font-size: 10px;
  text-transform: capitalize;
}

.card-body {
  font-size: 12px;
  line-height: 1.6;
  color: #333;
}

.card-footer {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
  font-size: 10px;
  color: #BBB;
}

.waiting-state {
  text-align: center;
  color: #999;
  padding: 40px 0;
  font-size: 12px;
}

.timeline-item-enter-active {
  transition: all 0.3s ease;
}

.timeline-item-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
</style>
