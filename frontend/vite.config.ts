import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({ plugins: [vue()], base: '/web/', server: { proxy: { '/api': 'http://127.0.0.1:8090' } } })
