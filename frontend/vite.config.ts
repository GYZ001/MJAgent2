/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5230,
    // 仅允许本机开发域名；禁止默认 allowedHosts:true 放大隧道暴露面（Todolist T7）
    allowedHosts: ['localhost', '127.0.0.1'],
    proxy: {
      '/api': 'http://127.0.0.1:8230',
      '/media': 'http://127.0.0.1:8230',
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
