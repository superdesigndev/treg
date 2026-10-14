import { execFileSync } from 'node:child_process'
import { defineConfig } from '@playwright/test'

// The browser tests' server takes a port the OS reports free when the run starts, so parallel runs
// (another worktree, another session, a `treg serve` proxy on its default 18791) never collide.
// The runner loads this file first and its workers inherit the chosen port through the environment.
// TREG_E2E_PORT pins one by hand.
if (!process.env.TREG_E2E_PORT) {
  process.env.TREG_E2E_PORT = execFileSync(process.execPath, ['-e',
    "const s=require('net').createServer();s.listen(0,'127.0.0.1',()=>{process.stdout.write(String(s.address().port));s.close()})",
  ]).toString().trim()
}
const origin = `http://127.0.0.1:${process.env.TREG_E2E_PORT}`

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: origin,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    // Use the full browser's headless mode: headless-shell does not exercise BFCache.
    channel: process.env.PLAYWRIGHT_CHANNEL || 'chromium',
  },
  webServer: {
    command: 'bash ../scripts/frontend-e2e-server.sh',
    url: `${origin}/meta`,
    reuseExistingServer: false,
    timeout: 90000,
  },
})
