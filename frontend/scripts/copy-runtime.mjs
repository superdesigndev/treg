// Standalone pages need Vue's global build; the maintained app uses Vite's npm import.
import { copyFileSync, mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const web = fileURLToPath(new URL('../../src/treg/web/', import.meta.url))
const vue = fileURLToPath(new URL('../node_modules/vue/', import.meta.url))
const { version } = JSON.parse(readFileSync(resolve(vue, 'package.json'), 'utf8'))
for (const page of ['enrich-arena.html', 'dashboard-legacy/index.html']) {
  const html = readFileSync(resolve(web, page), 'utf8')
  const url = html.match(/src="([^"]*\/vendor\/vue-([^/"]+)\.global\.prod\.js)"/)
  if (!url || url[2] !== version) throw new Error(`${page} must use the installed Vue ${version}`)
  const relative = url[1].replace(/^\/app\/legacy\//, 'dashboard-legacy/').replace(/^\//, '')
  const target = resolve(web, relative)
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(resolve(vue, 'dist/vue.global.prod.js'), target)
  copyFileSync(resolve(vue, 'LICENSE'), resolve(dirname(target), 'LICENSE'))
}
