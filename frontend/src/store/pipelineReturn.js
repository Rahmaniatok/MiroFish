// Remembers which pipeline run sent the user into the original MiroFish pages,
// so App.vue can offer a way back. sessionStorage: per tab, survives reloads.
const KEY = 'mirofish.pipelineReturn'

export function rememberPipelineRun(runId, name) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ runId, name }))
  } catch { /* storage blocked: the back link just won't show */ }
}

export function getPipelineRun() {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || 'null')
  } catch {
    return null
  }
}
