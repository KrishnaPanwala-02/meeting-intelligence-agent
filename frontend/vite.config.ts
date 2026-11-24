/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the Vite server forwards API calls to the backend, so the
// browser only ever talks to one origin (no CORS involved).
const backend = process.env.VITE_BACKEND_URL ?? 'http://localhost:8000'
const proxy = {
  '/api': { target: backend, changeOrigin: true },
  '/auth': { target: backend, changeOrigin: true },
  '/health': { target: backend, changeOrigin: true },
}

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy },
  preview: { port: 5173, strictPort: true, proxy },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    css: false,
  },
})
