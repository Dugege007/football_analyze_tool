import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

/**
 * 接口地址可配置：
 * - VITE_API_BASE：主后端（现网或你本机的只读库实例）地址，默认 http://127.0.0.1:8787。
 * - VITE_API_REPLICA_BASE：可选的只读副本实例地址，默认 http://127.0.0.1:8788；没有副本实例时可以不管。
 * - VITE_DEV_PORT：网页界面开发服务器端口，默认 5173。
 * 浏览器只访问开发服务器的 /api 与 /api-replica 前缀，由开发服务器代理到上面的地址，因此不需要后端额外放开跨源访问。
 * 配置方法：复制 web/.env.example 为 web/.env.local 后修改。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const apiBase = env.VITE_API_BASE || 'http://127.0.0.1:8787'
  const replicaBase = env.VITE_API_REPLICA_BASE || 'http://127.0.0.1:8788'
  const port = Number(env.VITE_DEV_PORT || 5173)
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port,
      proxy: {
        // 只读副本实例。必须写在 /api 前面，因为 '/api' 前缀也能匹配 '/api-replica'。
        '/api-replica': {
          target: replicaBase,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api-replica/, ''),
        },
        '/api': {
          target: apiBase,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
    preview: { host: '127.0.0.1', port: 4173 },
  }
})
