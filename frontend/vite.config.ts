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
  build: {
    // 构建产物落在 dist-staging，不是 dist——后端 app/main.py 用 StaticFiles 挂载
    // frontend/dist，而 StaticFiles 每次请求都读盘，所以写 dist 就是「即时发布到
    // 生产」，没有后端那种「改完必须手工重启才生效」的闸门。
    //
    // 2026-08-30 实测事故：并行 agent 把 `npm run build` 当自检手段跑，前端 18:06
    // 发布、后端进程还停在 14:50，用户拿到的是新前端配旧后端，小说导入预检直接挂掉。
    // 构建是每个人都会顺手跑的动作，不该带副作用。
    //
    // 发布改成显式一步：py scripts/publish_frontend.py（校验 + 原子替换）。
    outDir: 'dist-staging',
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
