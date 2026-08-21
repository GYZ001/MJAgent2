/// <reference types="vitest/config" />
import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'

function resolveAllowedHosts(): true | string[] {
  // 同域反代时浏览器 Host 为公网域名；后端仍用「Origin 与 Host 同源」闸门防跨站。
  // VITE_ALLOWED_HOSTS=localhost,127.0.0.1 可收回为显式列表；缺省 true 以便公网访问。
  const raw = (process.env.VITE_ALLOWED_HOSTS || '').trim()
  if (!raw || raw === 'true' || raw === 'all' || raw === '*') {
    return true
  }
  return raw.split(',').map((item) => item.trim()).filter(Boolean)
}

/** 把浏览器侧 Host 传给后端，供 Origin 同源校验（避免代理把 Host 改成 127.0.0.1）。 */
function backendProxy(): ProxyOptions {
  return {
    target: 'http://127.0.0.1:8230',
    changeOrigin: false,
    configure(proxy) {
      proxy.on('proxyReq', (proxyReq, req) => {
        const host = req.headers.host
        if (typeof host === 'string' && host) {
          proxyReq.setHeader('X-Forwarded-Host', host.split(',')[0].trim())
        }
      })
    },
  }
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5230,
    allowedHosts: resolveAllowedHosts(),
    proxy: {
      '/api': backendProxy(),
      '/media': backendProxy(),
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
