import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const appVersion = require('./package.json').version

let gitSha = 'unknown'
try {
  // Minimal, fixed environment (no inherited, potentially writeable PATH
  // entries): only the standard fixed system directories are searched, and
  // git is resolved with an absolute binary path. SonarCloud S4036.
  gitSha = execSync('git rev-parse --short HEAD', {
    stdio: ['ignore', 'pipe', 'ignore'],
    env: { PATH: '/usr/bin:/bin:/usr/local/bin' },
  })
    .toString()
    .trim()
} catch {
  gitSha = 'unknown'
}

// Injected as compile-time globals so the Sentry release can follow the
// backend convention: trading-platform-frontend@<version>+<git-sha>
export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
    __GIT_SHA__: JSON.stringify(gitSha),
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:5556',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:5556',
        ws: true,
      },
    },
  },
})
