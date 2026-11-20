// The standalone Arena page needs Vue's global build; the maintained app uses Vite's npm import.
import { copyFileSync, mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const web = fileURLToPath(new URL('../../src/treg/web/', import.meta.url))
const vue = fileURLToPath(new URL('../node_modules/vue/', import.meta.url))
const { version } = JSON.parse(readFileSync(resolve(vue, 'package.json'), 'utf8'))
const html = readFileSync(resolve(web, 'enrich-arena.html'), 'utf8')
const url = html.match(/src="([^"]*\/vendor\/vue-([^/"]+)\.global\.prod\.js)"/)
if (!url || url[2] !== version) throw new Error(`enrich-arena.html must use the installed Vue ${version}`)
const target = resolve(web, url[1].replace(/^\//, ''))
mkdirSync(dirname(target), { recursive: true })
copyFileSync(resolve(vue, 'dist/vue.global.prod.js'), target)
copyFileSync(resolve(vue, 'LICENSE'), resolve(dirname(target), 'LICENSE'))
