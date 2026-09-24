import { inject, markRaw, type ComponentPublicInstance, type InjectionKey } from 'vue'

// Compatibility boundary for the existing Options API feature modules. New components should
// expose typed props/events; the migrated pages share the original per-application state until
// their use cases can be moved independently. Never make this a module-level singleton.
type Dashboard = ComponentPublicInstance & Record<string, any>
const dashboardKey: InjectionKey<Dashboard> = Symbol('dashboard')

export function provideDashboard(vm: Dashboard) {
  return { [dashboardKey as symbol]: vm }
}

export function useDashboard() {
  const vm = inject(dashboardKey)
  if (!vm) throw new Error('Dashboard provider is missing')
  const bindings: Record<string, any> = {}
  const names = new Set([
    ...Object.keys(vm.$data),
    ...Object.keys(vm.$options.computed ?? {}),
    ...Object.keys(vm.$options.methods ?? {}),
  ])
  for (const name of names) {
    Object.defineProperty(bindings, name, {
      enumerable: true,
      get: () => vm[name],
      set: value => { vm[name] = value },
    })
  }
  bindings.$nextTick = vm.$nextTick.bind(vm)
  return bindings
}

export function createElements() {
  return markRaw({} as Record<string, Element | null>)
}
