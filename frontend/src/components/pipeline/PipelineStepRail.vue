<template>
  <ol class="step-rail" :class="{ compact }">
    <li
      v-for="(step, i) in PIPELINE_STEPS"
      :key="step.key"
      class="rail-step"
      :class="statusOf(step, i + 1)"
      :title="`${String(i + 1).padStart(2, '0')} · ${step.title}`"
    >
      <span class="rail-num">{{ String(i + 1).padStart(2, '0') }}</span>
      <span v-if="!compact" class="rail-title">{{ step.title }}</span>
      <span class="rail-bar"></span>
    </li>
  </ol>
</template>

<script setup>
import { PIPELINE_STEPS, uiStepStatus, uiCurrentStep } from './pipelineSteps'

const props = defineProps({
  // run manifest `steps` object ({key: {status}}); omit on the builder page
  steps: { type: Object, default: null },
  // step number highlighted as active when no manifest is given
  active: { type: Number, default: 1 },
  compact: { type: Boolean, default: false }
})

const statusOf = (step, num) => {
  if (!props.steps) return num === props.active ? 'running' : 'pending'
  const status = uiStepStatus(props.steps, step)
  if (status === 'completed') return 'done'
  if (status === 'running') return 'running'
  if (status === 'failed') return 'failed'
  if (status === 'paused' || num === uiCurrentStep({ steps: props.steps })) return 'next'
  return 'pending'
}
</script>

<style scoped>
.step-rail {
  list-style: none;
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 6px;
  width: 100%;
}
.rail-step {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.rail-num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  color: #BBB;
}
.rail-title {
  font-size: 10px;
  font-weight: 600;
  color: #999;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: 0.2px;
}
.rail-bar {
  height: 4px;
  border-radius: 2px;
  background: #EEE;
  position: relative;
  overflow: hidden;
}
.rail-step.done .rail-num, .rail-step.done .rail-title { color: #000; }
.rail-step.done .rail-bar { background: #000; }
.rail-step.running .rail-num, .rail-step.running .rail-title,
.rail-step.next .rail-num, .rail-step.next .rail-title { color: #FF5722; }
.rail-step.next .rail-bar { background: #FFD8CC; }
.rail-step.running .rail-bar { background: #FFD8CC; }
.rail-step.running .rail-bar::after {
  content: '';
  position: absolute;
  inset: 0;
  width: 40%;
  background: #FF5722;
  animation: sweep 1.4s ease-in-out infinite;
}
.rail-step.failed .rail-num, .rail-step.failed .rail-title { color: #F44336; }
.rail-step.failed .rail-bar { background: #F44336; }
.compact { gap: 3px; }
.compact .rail-num { font-size: 9px; }
.compact .rail-bar { height: 3px; }
@keyframes sweep { 0% { left: -40%; } 100% { left: 100%; } }
</style>
