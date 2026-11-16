import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// VITE_BASE: set when the dev server is reached through a path proxy
//   (e.g. code-server: VITE_BASE=/absproxy/5173/).
// VITE_ALLOWED_HOST: extra hostname allowed to reach the dev server.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    base: env.VITE_BASE || '/',
    plugins: [react(), tailwindcss()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      allowedHosts: env.VITE_ALLOWED_HOST ? [env.VITE_ALLOWED_HOST] : [],
      proxy: { [`${(env.VITE_BASE || '/').replace(/\/$/, '')}/api`]: {
        target: 'http://127.0.0.1:8000',
        rewrite: (p) => p.replace(/^.*?\/api/, '/api'),
      } },
    },
  }
})
