<script setup lang="ts">
import { computed } from 'vue'
import { LENS_ENGINES, type LensRun } from './types'
import { microUsd, publicSource } from './client'
const props = defineProps<{ run: LensRun }>()
const answers = computed(() => props.run.result?.results || [])
const mentioned = computed(() => answers.value.filter(a => a.brand_mentioned).length)
const sources = computed(() => new Set(answers.value.flatMap(a => a.citations.map(c => c.url))).size)
const engineName = (id: string) => LENS_ENGINES.find(e => e.id === id)?.label || id
const result = computed(() => props.run.result)
const date = (value: string) => new Date(value).toLocaleString()
</script>

<template>
  <section class="lens-results" aria-labelledby="lens-results-title">
    <div class="lens-section-head"><div><h2 id="lens-results-title">Audit results<span v-if="result?.input?.brand"> · {{result.input.brand}}</span></h2><p class="lens-hint">{{date(run.created_at)}} · <span class="lens-state" :class="run.state">{{run.state}}</span></p></div>
      <p class="lens-hint">{{result?.cost_known ? 'Charged' : 'Charge unconfirmed'}} <strong>{{microUsd(result?.total_cost_micro)}}</strong></p>
    </div>
    <p v-if="run.state==='running'" class="lens-notice" role="status">Your audit is running. Each completed answer will appear when the run is saved. You can return later without starting it again.</p>
    <p v-if="run.error" class="lens-error" role="alert">{{run.error}}</p>
    <div v-if="result && !result.cost_known" class="lens-notice">Confirmed charges: {{microUsd(result.known_cost_micro)}}. The final total is unconfirmed; unknown charges are not zero.</div>
    <div v-if="answers.length" class="lens-metrics">
      <div><span>Brand visibility</span><strong>{{Math.round(mentioned / answers.length * 100)}}<small>%</small></strong><p>{{mentioned}} of {{answers.length}} completed answers mention your brand</p></div>
      <div><span>Answer coverage</span><strong>{{answers.length}}</strong><p>{{new Set(answers.map(a => a.engine)).size}} answer engines represented</p></div>
      <div><span>Cited sources</span><strong>{{sources}}</strong><p>Unique URLs across completed answers</p></div>
    </div>
    <p v-if="answers.length" class="lens-hint">Visibility is measured in this run’s completed answers. Failed or missing answers are excluded. Model API and web-interface responses can differ.</p>
    <div v-if="result?.errors?.length" class="lens-error-list">
      <h3>Requests that need attention</h3>
      <p v-for="(error,index) in result.errors" :key="index"><strong>{{error.engine ? engineName(error.engine) + ': ' : ''}}</strong>{{error.detail}}<span v-if="error.question" class="lens-hint"> · {{error.question}}</span></p>
    </div>
    <div class="lens-answers">
      <details v-for="(answer,index) in answers" :key="answer.id" class="lens-answer" :open="index===0">
        <summary><span class="lens-answer-engine">{{engineName(answer.engine)}}</span><span class="lens-answer-question">{{answer.question}}</span><span class="lens-pill" :class="{positive:answer.brand_mentioned}">{{answer.brand_mentioned ? 'Brand mentioned' : 'No brand mention'}}</span></summary>
        <div class="lens-answer-body">
          <p class="lens-hint">{{answer.source_kind === 'model-api' ? 'Model API response' : 'Web-interface response'}} · {{answer.provider}} · {{microUsd(answer.charged_micro)}}<template v-if="answer.position!==null"> · Brand position {{answer.position}}</template></p>
          <p class="lens-answer-text">{{answer.answer}}</p><p v-if="answer.truncated" class="lens-notice">This answer or its citations were shortened to keep the saved evidence within its size limit.</p>
          <div v-if="answer.mentioned_brands.length" class="lens-hint">Brands mentioned: {{answer.mentioned_brands.join(', ')}}</div>
          <h4 v-if="answer.citations.length">Sources cited in this answer</h4>
          <ol v-if="answer.citations.length" class="lens-sources"><li v-for="(source,i) in answer.citations" :key="source.url+i"><a v-if="publicSource(source.url)" :href="publicSource(source.url)" target="_blank" rel="noopener noreferrer">{{source.title || source.domain || source.url}} ↗</a><span v-else>{{source.title || 'Source unavailable'}}</span><span class="lens-source-url">{{source.domain}}</span></li></ol>
          <p v-else class="lens-hint">This answer returned no citations.</p>
        </div>
      </details>
    </div>
    <p v-if="result && !answers.length && run.state!=='running'" class="lens-hint">This run returned no usable answers. The errors and request details remain available below.</p>
    <details v-if="result?.attempts?.length" class="lens-receipt"><summary>Request and charge details</summary>
      <div v-for="attempt in result.attempts" :key="attempt.id" class="lens-attempt"><strong>{{attempt.engine ? engineName(attempt.engine) : attempt.endpoint_id}}</strong><span>{{attempt.state}} · {{microUsd(attempt.charged_micro)}}</span><p v-if="attempt.detail">{{attempt.detail}}</p><code v-if="attempt.call_ref">{{attempt.call_ref}}</code></div>
    </details>
  </section>
</template>
