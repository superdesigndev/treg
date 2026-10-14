// The onboarding widgets, one source for two consumers:
// - the Dashboard imports these modules like any other component (compiled, no runtime templates);
// - `vite.agent-setup.config.ts` compiles this entry to the classic `/agent-setup.js` script, which
//   exposes `window.TregAgentSetup` to the standalone Enrich Arena page on its global Vue build.
export * from './data'
export { default as AgentPicker } from './AgentPicker.vue'
export { default as SetupInstructions } from './SetupInstructions.vue'
export { default as TryItOut } from './TryItOut.vue'
