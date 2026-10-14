// For editors running plain tsserver, which cannot read single-file components. `vue-tsc` resolves
// every `.vue` import to its real, typed component and never falls back to this declaration.
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, any>
  export default component
}
