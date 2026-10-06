import { defineConfig } from 'vitest/config'

export default defineConfig({
  server: { host: '127.0.0.1', strictPort: true, proxy: { '/api': 'http://127.0.0.1:3000' } },
  build: { outDir: 'dist', sourcemap: false },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'], restoreMocks: true, testTimeout: 15000 },
})
