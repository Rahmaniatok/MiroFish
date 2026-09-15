/**
 * Remembers the most recently built portfolio for the standalone
 * /dashboard/portfolio report page, independent of the step-flow shell's
 * own local state. Same reactive-singleton pattern as pendingUpload.js —
 * survives SPA navigation, not a hard page reload.
 */
import { reactive } from 'vue'

const state = reactive({
  portfolio: null
})

export function setLastPortfolio(portfolio) {
  state.portfolio = portfolio
}

export function getLastPortfolio() {
  return state.portfolio
}

export default state
