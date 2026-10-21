import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Chat from './Chat.vue'
import Catalog from './Catalog.vue'
import Community from './Community.vue'
import Assets from './Assets.vue'
import Profile from './Profile.vue'
import './style.css'
const router = createRouter({ history: createWebHashHistory(), routes: [
  { path:'/', redirect:'/chat' }, {path:'/chat',component:Chat}, {path:'/chat/:id',component:Chat},
  {path:'/shops',component:Catalog}, {path:'/shops/:id',component:Catalog},
  {path:'/community',component:Community}, {path:'/community/:id',component:Community},
  {path:'/orders',component:Assets}, {path:'/coupons',component:Assets},
    {path:'/users/:id',component:Profile},
  {path:'/:pathMatch(.*)*',redirect:'/chat'}
]})
createApp(App).use(createPinia()).use(router).mount('#app')
