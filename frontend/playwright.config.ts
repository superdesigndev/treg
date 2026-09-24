import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: 'http://127.0.0.1:18791',
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    // Use the full browser's headless mode: headless-shell does not exercise BFCache.
    channel: process.env.PLAYWRIGHT_CHANNEL || 'chromium',
  },
  webServer: {
    command: 'bash ../scripts/frontend-e2e-server.sh',
    url: 'http://127.0.0.1:18791/meta',
    reuseExistingServer: false,
    timeout: 90000,
  },
})
