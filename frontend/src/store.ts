import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from './api'
export const useAccount = defineStore('account', () => {
  const user = ref<any>(null), ready = ref(false), sessions = ref<any[]>([]), generation = ref(0)
  async function refreshSessions() { const g = generation.value; if (!user.value) return; const data = await api('/api/sessions'); if(g===generation.value) sessions.value=data.sessions || [] }
  function clear() { generation.value++; user.value=null; sessions.value=[] }
  async function boot() { try {user.value=(await api('/api/auth/me')).user;await refreshSessions()} catch { clear() } finally {ready.value=true} }
  async function login(email:string,code:string) {generation.value++;user.value=(await api('/api/auth/login','POST',{email,code})).user;await refreshSessions()}
  async function logout() {await api('/api/auth/logout','POST');clear()}
  return {user,ready,sessions,generation,boot,login,logout,refreshSessions,clear}
})
