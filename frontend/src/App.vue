<template>
  <router-view />
  <!-- news pipeline: way back from the original MiroFish pages to the run -->
  <button v-if="pipelineReturn" class="pipeline-return" @click="goBack">
    ↩ Pipeline run<span v-if="pipelineReturn.name"> · {{ pipelineReturn.name }}</span>
  </button>
</template>

<script setup>
// 使用 Vue Router 来管理页面
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getPipelineRun } from './store/pipelineReturn'

const route = useRoute()
const router = useRouter()
const ORIGINAL_FLOW = ['Process', 'Simulation', 'SimulationRun', 'Report', 'Interaction']

const pipelineReturn = computed(() => {
  if (!ORIGINAL_FLOW.includes(route.name)) return null
  return getPipelineRun()
})
const goBack = () => router.push(`/pipeline/${pipelineReturn.value.runId}`)
</script>

<style>
/* 全局样式重置 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: 'JetBrains Mono', 'Space Grotesk', 'Noto Sans SC', monospace;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #000000;
  background-color: #ffffff;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #333333;
}

/* 全局按钮样式 */
button {
  font-family: inherit;
}

.pipeline-return {
  position: fixed;
  /* in the empty space of the original pages' header, right of the MIROFISH brand */
  left: 150px;
  top: 14px;
  z-index: 1000;
  max-width: 320px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  background: #FF5722;
  color: #FFF;
  border: none;
  border-radius: 20px;
  padding: 8px 14px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2);
}
.pipeline-return:hover {
  background: #000;
}
</style>
