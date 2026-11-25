import { expect, test } from '@playwright/test'

// A browser whose WebGL runs on the CPU: no GPU, a VM, a remote desktop.
test.use({ launchOptions: { args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] } })

test('on a CPU-emulated GPU the landing shows its static hero instead of building WebGL scenes', async ({ page }) => {
  const three: string[] = []
  page.on('request', request => { if (request.url().includes('/three@')) three.push(request.url()) })
  await page.goto('/')
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'fallback')
  await expect(page.locator('.hcore')).toBeVisible()
  await expect(page.locator('html')).not.toHaveClass(/opening-pending/)
  await expect(page.locator('canvas')).toHaveCount(0)
  expect(three).toEqual([])
})
