/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5230,
    // 允许云环境通过 localtunnel / 临时公网域名访问开发服
    allowedHosts: true,
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
