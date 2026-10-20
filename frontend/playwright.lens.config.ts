import { defineConfig } from '@playwright/test'
import dashboard from './playwright.config'

// Separate server: no browser route mocks, test-only upstream responses, fresh real database.
export default defineConfig({
  ...dashboard,
  testDir: './e2e-live',
  outputDir: './test-results-lens',
  timeout: 60_000,
  webServer: {
    ...dashboard.webServer,
    command: 'bash ../scripts/frontend-lens-e2e-server.sh',
    url: `http://127.0.0.1:${process.env.TREG_E2E_PORT}/meta`,
    reuseExistingServer: false,
  },
})
