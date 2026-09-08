import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './style.css'
import Login from './views/Login.vue'
import Dash from './views/Dash.vue'
import Projects from './views/Projects.vue'
import AppMap from './views/AppMap.vue'
import Cases from './views/Cases.vue'
import Plans from './views/Plans.vue'
import Runs from './views/Runs.vue'
import Users from './views/Users.vue'
import Flows from './views/Flows.vue'
import LLM from './views/LLM.vue'
import { getToken } from './api'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login },
    { path: '/', component: Dash },
    { path: '/projects', component: Projects },
    { path: '/appmap', component: AppMap },
    { path: '/cases', component: Cases },
    { path: '/plans', component: Plans },
    { path: '/runs', component: Runs },
    { path: '/settings', component: LLM },
    { path: '/users', component: Users },
    { path: '/flows', component: Flows },
  ]
})
router.beforeEach((to) => {
  if (to.path !== '/login' && !getToken()) return '/login'
})

createApp(App).use(router).mount('#app')
