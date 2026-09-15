import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import Process from '../views/MainView.vue'
import SimulationView from '../views/SimulationView.vue'
import SimulationRunView from '../views/SimulationRunView.vue'
import ReportView from '../views/ReportView.vue'
import InteractionView from '../views/InteractionView.vue'
import DashboardShellView from '../views/dashboard/DashboardShellView.vue'
import FinalPortfolioView from '../views/dashboard/FinalPortfolioView.vue'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },
  {
    path: '/process/:projectId',
    name: 'Process',
    component: Process,
    props: true
  },
  {
    path: '/simulation/:simulationId',
    name: 'Simulation',
    component: SimulationView,
    props: true
  },
  {
    path: '/simulation/:simulationId/start',
    name: 'SimulationRun',
    component: SimulationRunView,
    props: true
  },
  {
    path: '/report/:reportId',
    name: 'Report',
    component: ReportView,
    props: true
  },
  {
    path: '/interaction/:reportId',
    name: 'Interaction',
    component: InteractionView,
    props: true
  },
  // Phase 7b — investment dashboard (Phase 1-6 services via the Phase 7a API),
  // rebuilt on MiroFish's own Graph/Split/Workbench shell (see MainView.vue)
  // instead of a generic redesign. One route: steps are internal state, same
  // as the old Process view's Step1-5 switching. None of the OASIS/ontology
  // routes above are touched.
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: DashboardShellView
  },
  {
    path: '/dashboard/portfolio',
    name: 'FinalPortfolio',
    component: FinalPortfolioView
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
