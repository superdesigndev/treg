<script>
import { useDashboard } from '../state/context'
import BrandMark from './BrandMark.vue'
// The landing page's top bar (src/treg/web/landing.html `.navwrap`), for /search: the two first
// screens a visitor meets should look like one site. Same links, same pill; a signed-in visitor
// gets their way into the dashboard instead of sign-in.
export default { components: { BrandMark }, setup: useDashboard }
</script>

<template>
<div class="lnav-wrap">
  <nav class="lnav" aria-label="treg">
    <a class="lnav-brand" href="/"><BrandMark/>treg</a>
    <div class="lnav-links">
      <a class="hidem ico" href="https://github.com/superdesigndev/treg" target="_blank" rel="noopener"><svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>Repo</a>
      <a class="hidem ico" href="https://discord.gg/6mQYYfFMAn" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20.32 4.37a19.8 19.8 0 0 0-4.89-1.52.07.07 0 0 0-.08.04c-.21.38-.44.87-.6 1.25a18.3 18.3 0 0 0-5.49 0 12.6 12.6 0 0 0-.61-1.25.08.08 0 0 0-.08-.04 19.7 19.7 0 0 0-4.88 1.52.07.07 0 0 0-.03.03C.53 9.05-.32 13.58.1 18.06c0 .02.01.04.03.05a19.9 19.9 0 0 0 6 3.03.08.08 0 0 0 .08-.03c.46-.63.87-1.3 1.22-2a.08.08 0 0 0-.04-.11 13.1 13.1 0 0 1-1.87-.89.08.08 0 0 1-.01-.13c.13-.09.25-.19.37-.29a.07.07 0 0 1 .08-.01c3.93 1.79 8.18 1.79 12.06 0a.07.07 0 0 1 .08.01c.12.1.24.2.37.29a.08.08 0 0 1-.01.13c-.6.35-1.22.64-1.87.89a.08.08 0 0 0-.04.11c.36.7.77 1.36 1.22 2a.08.08 0 0 0 .08.03 19.8 19.8 0 0 0 6.02-3.03.08.08 0 0 0 .03-.05c.5-5.18-.84-9.67-3.55-13.66a.06.06 0 0 0-.03-.03zM8.02 15.33c-1.18 0-2.16-1.08-2.16-2.42s.96-2.42 2.16-2.42c1.21 0 2.18 1.1 2.16 2.42 0 1.34-.96 2.42-2.16 2.42zm7.97 0c-1.18 0-2.16-1.08-2.16-2.42s.96-2.42 2.16-2.42c1.21 0 2.18 1.1 2.16 2.42 0 1.34-.95 2.42-2.16 2.42z"/></svg>Community</a>
      <a href="/search" aria-current="page" class="on">Tools</a>
      <a href="/catalog">Catalog</a>
      <!-- /search draws before the session is known: no Sign in that turns into Open dashboard. -->
      <a v-if="authed" class="lnav-candy" href="/app">Open dashboard</a>
      <template v-else-if="sessionChecked">
        <a class="hidexs" href="/app?ref=search" @click.prevent="openSignin()">Sign in</a>
        <button class="lnav-candy" type="button" @click="openSignin()">Start free</button>
      </template>
    </div>
  </nav>
</div>
</template>

<style scoped>
.lnav-wrap{position:fixed;top:0;left:0;right:0;z-index:50}
.lnav{display:flex;align-items:center;gap:22px;padding:24px 40px}
.lnav-brand{display:flex;align-items:center;gap:9px;font-family:"DM Mono",ui-monospace,"SF Mono",Menlo,monospace;font-weight:600;font-size:15px;color:#1a1a1a;text-decoration:none}
.lnav-brand .brand-mark{width:28px;height:28px;border-radius:8px}
.lnav-links{margin-left:auto;display:flex;align-items:center;gap:28px}
.lnav-links a{font-family:var(--sans);font-size:13.5px;font-weight:500;color:#7c7c7c;text-decoration:none;cursor:pointer;white-space:nowrap}
.lnav-links a:hover,.lnav-links a.on{color:#1a1a1a}
.lnav-links a.ico{display:inline-flex;align-items:center;gap:6px}
.lnav-links a.ico svg{width:15px;height:15px;display:block}
.lnav-links .lnav-candy{display:inline-block;font-family:var(--sans);font-weight:550;letter-spacing:.01em;
  color:#f8f8f7;background:#1a1a1a;border:0;border-radius:999px;padding:8px 18px;font-size:12.5px;cursor:pointer;
  box-shadow:0 1px 2px #00000014;transition:box-shadow .24s cubic-bezier(.2,.72,.25,1),transform .12s cubic-bezier(.22,1,.36,1)}
.lnav-links .lnav-candy:hover{color:#f8f8f7;transform:translateY(-1px);
  box-shadow:0 1px 2px -1px #0000000a,0 4px 6px -1px #0000000f,0 8px 16px #0000000a}
[data-theme="dark"] .lnav-brand,[data-theme="dark"] .lnav-links a:hover,[data-theme="dark"] .lnav-links a.on{color:#f2efe8}
[data-theme="dark"] .lnav-links .lnav-candy{background:#f2efe8;color:#151412}
@media(max-width:640px){
  .lnav{gap:12px;padding:14px 16px}.lnav-links{gap:12px}.lnav-links a{font-size:11px}
  .lnav-links .lnav-candy{padding:9px 13px;font-size:11px}
  .lnav-links a.hidem{display:none}   /* `a.ico` sets display too; this has to outrank it */
  .lnav-links .lnav-candy{white-space:nowrap}
}
/* Start free opens the same sign-in, so the narrowest phones keep one of the two. */
@media(max-width:400px){
  .lnav-links a.hidexs{display:none}
}
</style>
