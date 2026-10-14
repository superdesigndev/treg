<script>
import { useDashboard } from '../state/context'
import BrandMark from './BrandMark.vue'
export default { components: { BrandMark }, setup: useDashboard }
</script>

<template>
  <div class="lp">
    <header class="lp-nav">
      <a href="/" class="brand"><BrandMark/>treg</a>
      <nav class="lp-links">
        <a href="/tutorial">Docs</a>
        <button class="iconbtn" @click="toggleTheme" :aria-label="theme==='dark'?'Light theme':'Dark theme'">{{theme==='dark'?'◐':'◑'}}</button>
      </nav>
    </header>
    <main class="lp-hero" style="min-height:55vh">
      <template v-if="shareGate">
        <div class="lp-eyebrow">shared with you</div>
        <h1 class="lp-h1">{{shareGate.name}} <span style="color:var(--accent)">· {{shareGate.kind}}</span></h1>
        <p class="lp-lead">A teammate shared this {{shareGate.kind}} with you. Sign in to view it.</p>
      </template>
      <template v-else>
        <h1 class="lp-h1">Sign in to treg</h1>
        <p class="lp-lead">Connect your agent and manage your team.</p>
      </template>
      <div class="lp-cta"><button class="btn primary lg" @click="demo.signin=true">{{shareGate?'Sign in to view':'Sign in'}}</button></div>
    </main>
  </div>
</template>
