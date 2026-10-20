<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useDashboard } from '../state/context'
import { createLensState, canonicalWebsite } from '../lens/state'
import { microUsd, publicSource } from '../lens/client'
import { LENS_ENGINES, type LensRun } from '../lens/types'
import LensResults from '../lens/LensResults.vue'
import { DataTable, type Column } from '../components/ui/table'
import '../lens/lens.css'

const dashboard = useDashboard()
const scope = () => ({ user: String(dashboard.me || ''), team: String(dashboard.activeSlugNow || ''), enabled: !!dashboard.lensEnabled, canWrite: !!dashboard.canRegister, headers: dashboard.headers() as Record<string, string> })
const lens = createLensState(scope)
const s = lens.state
const heading = ref<HTMLElement | null>(null)
const reloadConfirm = ref(false)
const stepName = computed(() => ({ website: 'Start with your website', brand: 'Make it your brand', questions: 'Choose your buyer questions' })[s.draft.step])
const canWrite = computed(() => !!dashboard.canRegister)
const locked = computed(() => !!s.busy || !!s.draft.pending || s.draft.uncertainSave)
const historyColumns: Column<LensRun>[] = [
  { key: 'created_at', header: 'Started', value: r => new Date(r.created_at).toLocaleString(), mobile: 'primary' },
  { key: 'state', header: 'Status', value: r => r.state },
  { key: 'answers', header: 'Answers', value: r => r.result?.results?.length ?? '—', align: 'right' },
  { key: 'charged', header: 'Charged', value: r => microUsd(r.result?.total_cost_micro), align: 'right' },
]
watch(() => [dashboard.me, dashboard.activeSlugNow, dashboard.lensEnabled], () => { void lens.load() }, { immediate: true })
watch(() => s.draft.step, async () => { await nextTick(); heading.value?.focus() })
watch(() => [dashboard.activeSlugNow, s.draft.mode], () => { reloadConfirm.value = false })
watch(() => s.draft.mode, async () => { await nextTick(); heading.value?.focus() })
function beforeUnload(event: BeforeUnloadEvent) { if (s.busy === 'run' || s.busy === 'save') { event.preventDefault(); event.returnValue = '' } }
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { lens.pause(); window.removeEventListener('beforeunload', beforeUnload) })
function nextStep() {
  if (!s.draft.form.brand.trim() || !s.draft.form.url.trim()) { s.error = 'Add your brand name and website before continuing.'; return }
  s.error = ''; s.draft.step = 'questions';  lens.persist()
}
function save() {
  const selected = s.draft.form.questions.filter((_, i) => s.draft.selectedQuestions[i] !== false).map(q => q.trim()).filter(Boolean)
  if (!selected.length) { s.error = 'Choose at least one buyer question to track.'; return }
  s.draft.form.questions = selected; s.draft.selectedQuestions = selected.map(() => true); lens.persist(); void lens.save()
}
function websiteChanged(clearSuggestions = true) {
  if (s.draft.receipt && clearSuggestions) { s.draft.form.brand = ''; s.draft.form.description = ''; s.draft.form.competitors = []; s.draft.form.questions = ['']; s.draft.selectedQuestions = [true] }
  lens.changed()
}
function cancelEdit() { lens.leaveSetup() }
async function reloadProfile() {
  const id = s.draft.editingId
  if (!id) return
  await lens.openProject(id, true)
  if (s.project?.id === id && !s.error) { lens.begin(s.project); reloadConfirm.value = false }
}
function openAudit() { s.auditOpen = !s.auditOpen; lens.clearQuote(); s.error = '' }
function updateEngine(id: typeof LENS_ENGINES[number]['id'], checked: boolean) {
  s.engines = checked ? [...s.engines, id] : s.engines.filter(e => e !== id); lens.clearQuote()
}
function openRun(run: LensRun) { lens.selectRun(run) }
const research = computed(() => s.draft.receipt?.result)
const researchStale = computed(() => !!research.value && canonicalWebsite(String(research.value.input.url || '')) !== canonicalWebsite(s.draft.form.url))
</script>

<template>
  <div class="lens-page">
    <header class="lens-header">
      <div><p class="lens-eyebrow">AI search visibility</p><h1>Lens</h1><p class="lens-lead">Understand where your brand appears in the answers your buyers see.</p></div>
      <button v-if="canWrite && s.loaded && s.projects.length && s.draft.mode==='workspace'" class="btn primary" :disabled="!!s.busy || !!s.draft.pending" @click="lens.begin()">Add a brand</button>
    </header>
    <section v-if="!dashboard.lensEnabled" class="lens-panel"><h2>Lens is not enabled</h2><p>This deployment has not enabled Lens. Your existing tools are available from the navigation above.</p></section>
    <template v-else>
      <div v-if="s.error" class="lens-error" role="alert">{{s.error}}<button v-if="!s.loaded && !s.busy" class="btn sm" @click="lens.load()">Reload projects</button><button v-if="s.project && s.draft.mode==='workspace' && !s.busy" class="btn sm" @click="lens.openProject(s.project.id)">Reload project</button><button v-if="s.draft.editingId && !s.busy" class="btn sm" @click="reloadConfirm=true">Reload saved profile</button><div v-if="reloadConfirm" class="lens-notice">Replace your current edits with the saved team profile?<button class="btn sm" @click="reloadProfile">Replace edits</button><button class="btn sm" @click="reloadConfirm=false">Keep editing</button></div></div>
      <p v-if="s.notice" class="lens-notice" role="status">{{s.notice}}</p>
      <div v-if="s.draft.pending" class="lens-pending" role="status"><div><strong>{{s.busy==='run' ? 'Your request is running' : 'Your request needs a status check'}}</strong><p>Its ID is saved. Leaving or reloading this page will not submit another paid request.</p></div><button class="btn" :disabled="!!s.busy" @click="lens.readRun(s.draft.pending.id)">{{s.busy==='run' ? 'Checking status…' : 'Check request status'}}</button></div>
      <div v-if="canWrite && s.draft.uncertainSave && !s.busy" class="lens-notice">The profile save has not been confirmed. Your draft is locked to preserve that exact request. <button class="btn sm" @click="lens.save()">Check and finish saving</button></div>
      <p v-if="!s.loaded && s.busy==='loading'" role="status" class="lens-loading">Loading your team’s brand projects…</p>
      <template v-if="s.loaded"><p v-if="!canWrite" class="lens-notice">You have read-only access to this team. You can inspect saved projects and audit results; a team member can create projects or run research.</p>
        <div v-if="s.draft.mode==='setup' && canWrite" class="lens-setup">
          <aside class="lens-setup-aside"><p class="lens-eyebrow">{{s.draft.editingId ? 'Edit your brand' : 'Your next clear picture'}}</p><h2>From your website<br>to buyer answers.</h2><p>Bring your brand into focus, choose the questions that matter, then see what AI says.</p>
            <ol class="lens-steps" aria-label="Brand setup progress"><li v-for="(step,i) in ['website','brand','questions']" :key="step" :class="{active:s.draft.step===step}" :aria-current="s.draft.step===step?'step':undefined"><span>{{i+1}}</span>{{['Your website','Your brand','Buyer questions'][i]}}</li></ol>
            <div class="lens-team-note">Saved to <strong>{{dashboard.activeName}}</strong><br>Your team’s existing permissions and billing apply.</div>
          </aside>
          <section class="lens-panel lens-setup-form" aria-labelledby="lens-step-title" :aria-busy="!!s.busy">
            <div class="lens-section-head"><div><p class="lens-eyebrow">Step {{s.draft.step==='website'?1:s.draft.step==='brand'?2:3}} of 3</p><h2 id="lens-step-title" ref="heading" tabindex="-1">{{stepName}}</h2></div><button v-if="s.project && !locked" class="btn sm" @click="cancelEdit">Back to project</button></div>
            <p v-if="researchStale" class="lens-notice">Your research was for {{research?.input.url}}. Your current website has not been researched; review the brand, competitors and questions before saving.</p>
            <template v-if="s.draft.step==='website'">
              <p class="lens-hint">We can research your site and suggest competitors and questions. You’ll review everything before saving.</p>
              <form @submit.prevent="lens.estimate('onboarding')"><label class="lens-field"><span>Website URL</span><input v-model="s.draft.form.url" type="text" aria-label="Website URL" inputmode="url" autocomplete="url" placeholder="yourbrand.com" maxlength="2048" required :disabled="locked" @input="websiteChanged()"><small>A public business website. A domain is enough.</small></label>
                <button v-if="!s.quote" class="btn primary lens-wide" :disabled="locked || !s.draft.form.url.trim()">{{s.busy==='quote' ? 'Getting your estimate…' : 'Get research estimate'}}</button>
              </form>
              <div v-if="s.quote?.kind==='onboarding'" class="lens-quote" aria-label="Research estimate"><div><span>Estimated research charge</span><strong>{{microUsd(s.quote.total_cost_micro)}}</strong></div><p>One fixed-price research request. Your own provider key, if connected, is billed separately by the provider. This estimate expires at {{new Date(s.quote.expires_at).toLocaleTimeString()}}. Saving the profile is free.</p><p v-if="s.quote.note">{{s.quote.note}}</p><button class="btn primary lens-wide" :disabled="locked" @click="lens.start()">Research my brand — {{microUsd(s.quote.total_cost_micro)}}</button></div>
              <div class="lens-divider"><span>or start with what you know</span></div><button class="btn lens-wide" :disabled="locked" @click="lens.manual()">Set up manually — free</button>
              <p class="lens-footnote">Research suggestions can be imperfect. You control which brands and questions are saved. No audit starts automatically.</p>
            </template>
            <template v-else-if="s.draft.step==='brand'">
              <p class="lens-hint">Use the brand name buyers recognize. Add the alternatives they compare you with.</p>
              <label class="lens-field"><span>Brand name</span><input v-model="s.draft.form.brand" maxlength="100" autocomplete="organization" :disabled="locked" @input="lens.changed()"></label>
              <label class="lens-field"><span>Brand website</span><input v-model="s.draft.form.url" maxlength="2048" inputmode="url" :disabled="locked" @input="websiteChanged(false)"></label>
              <label class="lens-field"><span>What does your brand do? <small>Optional</small></span><textarea v-model="s.draft.form.description" rows="3" maxlength="1200" :disabled="locked" @input="lens.changed()"></textarea></label>
              <div class="lens-field-head"><h3>Competitors</h3><span class="lens-hint">{{s.draft.form.competitors.length}} of 8</span></div>
              <p v-if="!s.draft.form.competitors.length" class="lens-hint">Add a competitor, or continue with your brand alone.</p>
              <div v-for="(competitor,i) in s.draft.form.competitors" :key="i" class="lens-competitor"><div class="lens-competitor-inputs"><label class="lens-field"><span>Competitor {{i+1}}</span><input v-model="competitor.name" maxlength="100" :disabled="locked" @input="delete competitor.reason; delete competitor.domain; lens.changed()"></label><label class="lens-field"><span>Website <small>Optional</small></span><input v-model="competitor.domain" maxlength="253" :disabled="locked" @input="lens.changed()"></label></div><button class="btn sm" :aria-label="'Remove competitor '+(i+1)" :disabled="locked" @click="s.draft.form.competitors.splice(i,1); lens.changed()">Remove</button><p v-if="competitor.reason" class="lens-hint">{{competitor.reason}}</p></div>
              <button class="btn sm" :disabled="locked || s.draft.form.competitors.length>=8" @click="s.draft.form.competitors.push({name:''}); lens.changed()">Add competitor</button>
              <div class="lens-form-actions"><button v-if="!s.draft.editingId" class="btn" :disabled="locked" @click="s.draft.step='website'; lens.persist()">Back</button><button class="btn primary" :disabled="locked" @click="nextStep">Choose buyer questions</button></div>
            </template>
            <template v-else>
              <p class="lens-hint">Track the questions a buyer might ask before choosing a product like yours. Suggestions are generated ideas, not observed search demand.</p>
              <div v-for="(_,i) in s.draft.form.questions" :key="i" class="lens-question"><label class="lens-question-check"><input :checked="s.draft.selectedQuestions[i]!==false" @change="s.draft.selectedQuestions[i]=($event.target as HTMLInputElement).checked; lens.persist()" type="checkbox" :aria-label="'Track buyer question '+(i+1)" :disabled="locked"></label><label class="lens-field"><span>Buyer question {{i+1}}</span><textarea v-model="s.draft.form.questions[i]" rows="2" maxlength="500" :disabled="locked" @input="lens.changed()"></textarea></label><button class="btn sm" :aria-label="'Remove question '+(i+1)" :disabled="locked" @click="s.draft.form.questions.splice(i,1); s.draft.selectedQuestions.splice(i,1); lens.changed()">Remove</button></div>
              <button class="btn sm" :disabled="locked || s.draft.form.questions.length>=8" @click="s.draft.form.questions.push(''); s.draft.selectedQuestions.push(true); lens.changed()">Add a question</button>
              <div class="lens-form-actions"><button class="btn" :disabled="locked" @click="s.draft.step='brand'; lens.persist()">Back</button><button class="btn primary" :disabled="locked" @click="save">{{s.busy==='save'?'Saving your brand…':'Save brand profile'}}</button></div><p class="lens-footnote">Your next step is a separate audit estimate. Saving this profile makes no paid calls.</p>
            </template>
            <details v-if="research" class="lens-receipt"><summary>Research sources and receipt</summary><p>Researched website: {{research.input.url}}</p><p v-if="researchStale" class="lens-notice">This research belongs to a different website. Your current website has not been researched. Review the brand, competitors and questions before saving.</p><p>{{research.cost_known?'Charged':'Final charge unconfirmed'}}: {{microUsd(research.total_cost_micro)}}<template v-if="!research.cost_known"> · Confirmed so far {{microUsd(research.known_cost_micro)}}</template></p><p v-for="(error,i) in research.errors" :key="i" class="lens-error">{{error.detail}}</p><ol class="lens-sources"><li v-for="(source,i) in research.research?.sources || []" :key="i"><a v-if="publicSource(source.url)" :href="publicSource(source.url)" target="_blank" rel="noopener noreferrer">{{source.title || source.url}} ↗</a><span v-else>{{source.title}}</span></li></ol></details>
          </section>
        </div>
        <template v-else-if="s.project">
          <section class="lens-project-bar" aria-label="Brand project"><div><label for="lens-project">Brand project</label><select id="lens-project" :value="s.project.id" :disabled="!!s.busy || !!s.draft.pending" @change="lens.openProject(($event.target as HTMLSelectElement).value)"><option v-for="project in s.projects" :key="project.id" :value="project.id">{{project.brand}}</option></select></div><div class="lens-project-site"><strong>{{s.project.brand}}</strong><a :href="publicSource(s.project.url)" target="_blank" rel="noopener noreferrer">{{s.project.url}} ↗</a></div><button v-if="s.project.can_edit && canWrite" class="btn" :disabled="!!s.busy || !!s.draft.pending" @click="lens.begin(s.project)">Edit brand</button></section>
          <section class="lens-panel lens-project-summary"><div><h2>Your brand, in the answer</h2><p>{{s.project.description || 'Compare how answer engines describe your brand and its alternatives.'}}</p><div class="lens-inline-stats"><span><b>{{s.project.questions.length}}</b> buyer questions</span><span><b>{{s.project.competitors.length}}</b> competitors</span><span><b>{{s.runs.length}}</b> saved audits</span></div></div><button v-if="canWrite" class="btn primary" :disabled="!!s.busy || !!s.draft.pending" @click="openAudit">{{s.auditOpen?'Close audit setup':s.runs.length?'Run a new audit':'Review your first audit'}}</button></section>
          <section v-if="s.auditOpen && canWrite" class="lens-panel lens-audit-setup" aria-labelledby="lens-audit-title"><div class="lens-section-head"><div><p class="lens-eyebrow">A fresh view</p><h2 id="lens-audit-title">Review your audit</h2></div><span class="lens-pill">{{s.project.questions.length * s.engines.length}} answers requested</span></div><p class="lens-hint">Each selected engine answers your saved buyer questions. Review the estimated charge before any request is sent.</p><fieldset class="lens-engine-options" :disabled="!!s.busy || !!s.draft.pending"><legend>Answer engines</legend><label v-for="engine in LENS_ENGINES" :key="engine.id"><input type="checkbox" :checked="s.engines.includes(engine.id)" @change="updateEngine(engine.id,($event.target as HTMLInputElement).checked)">{{engine.label}}</label></fieldset><details class="lens-receipt"><summary>{{s.project.questions.length}} buyer questions in this audit</summary><ol class="lens-sources"><li v-for="question in s.project.questions" :key="question">{{question}}</li></ol></details><button v-if="!s.quote" class="btn primary" :disabled="!!s.busy || !!s.draft.pending || !s.engines.length || !s.project.questions.length" @click="lens.estimate('audit')">{{s.busy==='quote'?'Getting audit estimate…':'Get audit estimate'}}</button><div v-if="s.quote?.kind==='audit'" class="lens-quote"><div><span>Estimated audit charge</span><strong>{{microUsd(s.quote.total_cost_micro)}}</strong></div><p>Paid by {{dashboard.activeName}}. Actual provider charges may vary from this estimate. Your own provider keys are billed separately by that provider.</p><p v-if="s.quote.note">{{s.quote.note}}</p><button class="btn primary" :disabled="!!s.busy || !!s.draft.pending" @click="lens.start()">Run audit — estimate {{microUsd(s.quote.total_cost_micro)}}</button></div></section>
          <button v-if="s.activeRun?.state==='running' && !s.draft.pending" class="btn" :disabled="!!s.busy" @click="lens.readRun(s.activeRun.id)">Check audit status</button>
          <LensResults v-if="s.activeRun" :run="s.activeRun" />
          <section v-else class="lens-empty"><div class="lens-empty-mark" aria-hidden="true">↗</div><h2>Your first answers start here</h2><p>Your brand profile is ready. Review your first audit to see real answers, mentions and citations.</p></section>
          <section v-if="s.runs.length" class="lens-history"><h2>Audit history</h2><p class="lens-hint">Each audit keeps its original questions and results. Select a run to inspect it.</p><DataTable :columns="historyColumns" :rows="s.runs" :row-key="row=>row.id" :selected="s.activeRun?.id" :interactive="!s.busy && !s.draft.pending" :surface="true" @row-click="openRun" /></section>
        </template>
        <section v-else-if="!canWrite" class="lens-empty"><h2>No brand projects yet</h2><p>A team member can add the first brand project. Saved results will appear here.</p></section>
      </template>
    </template>
  </div>
</template>
