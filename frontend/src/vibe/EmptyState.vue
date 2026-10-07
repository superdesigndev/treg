<script setup lang="ts">
// A new conversation: what vibe-it does in one line, ideas that play to the catalog's strengths, and
// the team's published tools to start from.
defineProps<{ tools: { tool_id: string, version: number, status: string, summary: string }[],
  drafts: { id: number, title: string, name?: string | null, updated_at: string }[] }>()
const emit = defineEmits<{ idea: [text: string], fromTool: [id: string], fromDraft: [id: number] }>()

function day(iso: string): string {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const IDEAS = [
  { title: 'Company snapshot', text: 'A tool that takes a company domain and returns its social links, employee count and a one-line description.' },
  { title: 'Brand mentions', text: 'Find recent Reddit and Hacker News posts that mention my product name, newest first.' },
  { title: 'Email list check', text: 'Given a list of emails, tell me which ones are deliverable, as a table.' },
  { title: 'SEO quick look', text: 'Take a domain and return its top organic keywords and backlink count.' },
  { title: 'Lead finder', text: 'Find the head of marketing at a company, with a verified work email.' },
  { title: 'Creator stats', text: 'Take a TikTok or YouTube handle and return followers, average views and recent posts.' },
]
</script>

<template>
  <div class="vb-intro">
    <h1>What do you want to build?</h1>
    <p class="sa-muted">Describe it in plain words. The agent finds the catalog tools for each step, writes the files and shows
      you every change; you run a test, publish, and turn on a web page for it if you like.</p>
    <ol class="vb-how">
      <li><b>Describe</b> what goes in and what comes out</li>
      <li><b>Review</b> the steps, prices and files</li>
      <li><b>Test</b> on your balance</li>
      <li><b>Publish</b>, then share an app</li>
    </ol>
    <h2 class="vb-gap">Ideas</h2>
    <ul class="vb-ideas">
      <li v-for="i in IDEAS" :key="i.title"><button type="button" @click="emit('idea', i.text)"><b>{{ i.title }}</b><span class="sa-muted">{{ i.text }}</span></button></li>
    </ul>
    <div id="vb-change"/>
    <template v-if="tools.length">
      <h2 class="vb-gap">Change one of your tools</h2>
      <ul class="vb-mine">
        <li v-for="t in tools" :key="t.tool_id">
          <button type="button" @click="emit('fromTool', t.tool_id)">
            <code>{{ t.tool_id }}</code> <span class="sa-muted">v{{ t.version }} · {{ t.status }}</span>
            <span class="sa-muted vb-mine-sum">{{ t.summary }}</span>
          </button>
        </li>
      </ul>
    </template>
    <template v-if="drafts.length">
      <h2 class="vb-gap">Continue a draft</h2>
      <p class="sa-muted vb-small vb-sub">Not published yet. A copy of its files starts here; the original chat stays as it is.</p>
      <ul class="vb-mine">
        <li v-for="d in drafts" :key="d.id">
          <button type="button" @click="emit('fromDraft', d.id)">
            <span><b>{{ d.title }}</b> <span v-if="d.name && d.name !== d.title" class="sa-muted"><code>{{ d.name }}</code></span></span>
            <span class="sa-muted vb-mine-sum">draft · {{ day(d.updated_at) }}</span>
          </button>
        </li>
      </ul>
    </template>
    <p v-if="!tools.length && !drafts.length" id="vb-nothing" class="sa-muted vb-small vb-gap">Your published tools and unpublished drafts will show here to change or continue.</p>
  </div>
</template>
