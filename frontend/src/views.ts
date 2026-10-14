import { defineAsyncComponent, type Component, type ComponentPublicInstance } from 'vue'
import controller from './state/controller.js'

// Pages and dialogs load on demand, so the first paint downloads the app shell and state, not
// every screen. Two things keep that invisible to the user:
// 1. `preloadInitialView` starts the chunk for the URL being opened as soon as the entry runs,
//    in parallel with boot's /meta and /auth/me, so the first view is ready when boot finishes.
// 2. `prefetchAfterBoot` loads the rest of the reachable screens in idle time after boot. A
//    component loaded that way renders synchronously when it is next shown, like a static import.
// A dialog whose chunk arrives late still behaves: `v-dialog` (dialogs/dialog.ts) takes focus and
// records the opener when the dialog mounts, not when its flag flips.

type Module = { default: Component }
type Dashboard = ComponentPublicInstance & Record<string, any>
// `__asyncLoader` is the async wrapper's own loader: calling it resolves the wrapper itself, which
// is what makes a prefetched screen render without a blank frame. Vue's lazy hydration and Nuxt's
// component preloading use the same hook.
type Lazy = Component & { __asyncLoader: () => Promise<Component> }

let root: Dashboard | null = null

function lazy(load: () => Promise<Module>): Lazy {
  return defineAsyncComponent({
    loader: () => load().then(m => m.default),
    onError(error, retry, fail, attempts) {
      if (attempts <= 2) return retry()
      // A chunk that cannot be fetched usually means a deploy replaced this build: the version
      // check offers the refresh toast when that is the case.
      root?.checkVersion?.()
      fail()
      console.error('Dashboard view failed to load', error)
    },
  }) as Lazy
}

// Classic scripts that only the Help view reads (`window.TREG_TUTORIAL`, `tregHL`, `TREG_TOUR`).
// They are served no-cache and shared with the standalone /tutorial and /dashboard-tour pages.
const scripts = new Map<string, Promise<void>>()
export function loadScript(src: string): Promise<void> {
  let loading = scripts.get(src)
  if (!loading) {
    loading = new Promise(resolve => {
      const script = document.createElement('script')
      script.src = src
      script.onload = () => resolve()
      // The Help view has empty fallbacks for missing data, as it did when the tag failed.
      script.onerror = () => { scripts.delete(src); console.error('Failed to load', src); resolve() }
      document.head.appendChild(script)
    })
    scripts.set(src, loading)
  }
  return loading
}

export const pages = {
  catalog: lazy(() => import('./pages/CatalogPage.vue')),
  connections: lazy(() => import('./pages/ConnectionsPage.vue')),
  find: lazy(() => import('./pages/SearchPage.vue')),
  provider: lazy(() => import('./pages/ProviderPage.vue')),
  platform: lazy(() => import('./pages/PlatformPage.vue')),
  tools: lazy(() => import('./pages/ToolsPage.vue')),
  detail: lazy(() => import('./pages/DetailPage.vue')),
  secrets: lazy(() => import('./pages/SecretsPage.vue')),
  resources: lazy(() => import('./pages/TeamResourcesPage.vue')),
  orgs: lazy(() => import('./pages/TeamPage.vue')),
  activity: lazy(() => import('./pages/ActivityPage.vue')),
  admin: lazy(() => import('./pages/AdminPage.vue')),
  start: lazy(() => import('./pages/GettingStartedPage.vue')),
  referrals: lazy(() => import('./pages/ReferralsPage.vue')),
  hub: lazy(() => import('./pages/HubPage.vue')),
  run: lazy(() => import('./pages/HubRunPage.vue')),
  help: lazy(() => Promise.all([
    import('./pages/HelpPage.vue'), loadScript('/tutorial.js'), loadScript('/dashboard-tour/tour.js'),
  ]).then(([page]) => page)),
}
type View = keyof typeof pages

export const dialogs = {
  OnboardingFlow: lazy(() => import('./onboarding/OnboardingFlow.vue')),
  FishVoiceDialog: lazy(() => import('./dialogs/FishVoiceDialog.vue')),
  ConnectTokenDialog: lazy(() => import('./dialogs/ConnectTokenDialog.vue')),
  TopUpDialog: lazy(() => import('./dialogs/TopUpDialog.vue')),
  AgentGuideDialog: lazy(() => import('./dialogs/AgentGuideDialog.vue')),
  ConnectionMethodDialog: lazy(() => import('./dialogs/ConnectionMethodDialog.vue')),
  ResourcePickerDialog: lazy(() => import('./dialogs/ResourcePickerDialog.vue')),
  ExtraCredentialDialog: lazy(() => import('./dialogs/ExtraCredentialDialog.vue')),
  EditToolDialog: lazy(() => import('./dialogs/EditToolDialog.vue')),
  AcceptInvitesDialog: lazy(() => import('./dialogs/AcceptInvitesDialog.vue')),
  WelcomeDialog: lazy(() => import('./dialogs/WelcomeDialog.vue')),
  CopyToolDialog: lazy(() => import('./dialogs/CopyToolDialog.vue')),
  ImportSkillDialog: lazy(() => import('./dialogs/ImportSkillDialog.vue')),
  RequestToolDialog: lazy(() => import('./dialogs/RequestToolDialog.vue')),
  ShareDialog: lazy(() => import('./dialogs/ShareDialog.vue')),
  RecipeDialog: lazy(() => import('./dialogs/RecipeDialog.vue')),
  RunToolDialog: lazy(() => import('./dialogs/RunToolDialog.vue')),
  CallDetailsDialog: lazy(() => import('./dialogs/CallDetailsDialog.vue')),
  TryEndpointDialog: lazy(() => import('./dialogs/TryEndpointDialog.vue')),
}

function prefetch(component: Lazy) {
  return component.__asyncLoader().catch(() => {})
}

// The view boot will open for this URL, read with the same route parsers boot uses. A wrong guess
// costs one early download; a signed-out /app visit shows the sign-in page, which is not lazy.
function initialView(): View {
  const route = controller.methods as Record<string, (...args: any[]) => any>
  const path = location.pathname
  const catalog = route.catalogFromPath!(path)
  if (catalog) return catalog.view
  if (route.runFromPath!(path)) return 'run'
  if (route.mkFromPath!(path)) return 'provider'
  if (route.routeFromPath!(path)) return 'detail'
  if (route.platformFromHash!()) return 'platform'
  return route.viewFromHash!.call({}) || 'start'
}

export function preloadInitialView() {
  void prefetch(pages[initialView()])
}

const idle = () => new Promise<void>(resolve => {
  if ('requestIdleCallback' in window) requestIdleCallback(() => resolve(), { timeout: 2000 })
  else setTimeout(resolve, 50)
})

// After boot, fetch what this visitor can reach next, one chunk per idle period. A member gets
// every screen and dialog except Help, which is rarely opened and would pull the tutorial scripts
// in too; a public catalog or /search visitor gets only the catalog pages.
export function prefetchAfterBoot(vm: Dashboard) {
  root = vm
  const start = async () => {
    if (vm.bootFailed) return
    const member = vm.authed && !vm.publicCatalog
    const views: View[] = member
      ? ['start', 'catalog', 'connections', 'tools', 'activity', 'orgs', 'platform', 'provider', 'detail', 'secrets',
         'resources', 'referrals', 'hub', 'run', ...(vm.isAdmin ? ['admin' as const] : [])]
      : ['catalog', 'platform']
    const queue: Lazy[] = [...views.map(v => pages[v]), ...(member ? Object.values(dialogs) : [])]
    for (const component of queue) { await idle(); await prefetch(component) }
  }
  // /search marks itself ready before the entry's mount call returns; every other URL after boot.
  if (vm.bootReady) { void start(); return }
  const stop = vm.$watch('bootReady', (ready: boolean) => { if (ready) { stop(); void start() } })
}
