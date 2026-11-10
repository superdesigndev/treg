<script>
import { useDashboard } from '../state/context'
import ToolDrawer from '../components/ToolDrawer.vue'
import ProviderLogo from '../components/ProviderLogo.vue'
import ConnectionCard from '../components/ConnectionCard.vue'
export default { components: { ToolDrawer, ProviderLogo, ConnectionCard }, setup: useDashboard }
</script>

<template>
<!-- Paused on this deployment: out of /oauth/providers, so only /meta names it. The team's
     connections are kept and listed; nothing here can connect. -->
<div v-if="!mkProvider && mkPaused" class="pl pv">
  <header class="pl-hero pv-hero">
    <nav class="pl-crumbs" aria-label="Breadcrumb"><a href="/app#connections" @click.prevent="go('connections')">Connections</a><span>/</span>{{mkPaused.display_name}}</nav>
    <div class="pv-id">
      <ProviderLogo :service="mkService" large />
      <h1>{{mkPaused.display_name}}</h1>
    </div>
  </header>
  <p class="mk-notice">{{mkPaused.message}}</p>
  <section class="pl-sec" v-if="mkAccounts.length">
    <h2 class="pl-h"><span>Connected</span><i></i><em>{{mkAccounts.length}}</em></h2>
    <div class="pl-grid pl-grid-t">
      <ConnectionCard v-for="a in mkAccounts" :key="a.id" :a="a" />
    </div>
  </section>
</div>
<div v-else class="pl pv" :class="{dopen:!!drawerEp}">
          <header class="pl-hero pv-hero">
            <nav class="pl-crumbs" aria-label="Breadcrumb"><a href="/app#connections" @click.prevent="go('connections')">Connections</a><span>/</span>{{mkProvider.display_name}}</nav>
            <div class="pv-id">
              <ProviderLogo :service="mkProvider.service" large />
              <h1>{{mkProvider.display_name}}</h1>
            </div>
            <p class="pl-lede">{{mkProvider.summary}}</p>
            <div class="pv-acts">
              <button class="pl-btn" :disabled="!mkProvider.configured || connBusy" @click="startConnect(mkProvider)">
                {{connectLabel(mkProvider, mkAccounts.length)}}</button>
              <a v-if="mkProvider.docs_url" class="pl-btn ghost" :href="mkProvider.docs_url" target="_blank" rel="noopener">API docs ↗</a>
            </div>
            <!-- Shown everywhere Connect can be clicked, before the consent popup opens. -->
            <p v-if="mkProvider.consent_notice" class="mk-notice">{{mkProvider.consent_notice}}</p>
          </header>

          <div v-if="connErr || secretErr" class="banner cn-banner"><span>{{connErr || secretErr}}</span><button class="btn sm ico" @click="connErr=''; secretErr=''" aria-label="Dismiss">✕</button></div>
          <div v-if="!mkProvider.configured" class="banner cn-banner">
            This server holds no client credentials for {{mkProvider.display_name}}, so the connect flow can't run here.
          </div>

          <section class="pl-sec" v-if="mkAccounts.length">
            <h2 class="pl-h"><span>Connected</span><i></i><em>{{mkAccounts.length}}</em></h2>
            <div class="pl-grid pl-grid-t">
              <ConnectionCard v-for="a in mkAccounts" :key="a.id" :a="a" />
            </div>
          </section>

          <!-- Every tool this provider serves, by platform. A tool that is one of several providers
               doing the same thing links to that capability's comparison: from "what does my key do" to
               "who else does this, and how do they compare". -->
          <section class="pl-sec" v-if="mkToolShelves.length || (mkTools && mkTools.loading)">
            <h2 class="pl-h"><span>Tools</span><i></i><em v-if="mkToolCount">{{mkToolCount}}</em></h2>
            <p class="cat-hint">What an agent can call on {{mkProvider.display_name}}, by platform.</p>
            <div v-if="mkTools && mkTools.loading && !mkToolShelves.length" class="pl-empty">Loading…</div>
            <div v-else-if="mkTools && mkTools.err" class="pl-empty">{{mkTools.err}}</div>
            <div v-for="p in mkToolShelves" :key="p.slug" class="pv-shelf" v-show="p.tools.length">
              <h3 class="pl-h pl-h-quiet"><a :href="platUrl(p.slug)" @click.prevent="openPlatform(p.slug)">{{p.label}}</a><em>{{p.tools.length}}</em></h3>
              <div class="pl-grid pl-grid-t">
                <div v-for="t in p.tools" :key="t.id" class="pl-card pl-tool" :class="{on:drawerTool===t.id}" role="button" tabindex="0"
                     @click="openTool(t.id)" @keydown.enter="openTool(t.id)">
                  <span class="pl-tool-b"><b>{{t.title}}</b>
                    <span class="pl-meta">{{toolPrice(t.e)}}<template v-if="t.compare"> · <a class="pv-cmp"
                      :href="platUrl(p.slug, t.compare.key)" @click.stop.prevent="openComparisonOn(p.slug, t.compare.key)" :title="t.compare.description">compare with {{t.compare.providers-1}} other{{t.compare.providers>2?'s':''}}</a></template></span></span>
                </div>
              </div>
            </div>
            <div v-if="mkPlumbCount" class="pv-shelf">
              <h3 class="pl-h pl-h-quiet"><span>Account and setup</span><em>{{mkPlumbCount}}</em></h3>
              <div class="pl-grid pl-grid-t">
                <template v-for="p in mkToolShelves" :key="'x'+p.slug">
                  <button v-for="t in p.plumbing" :key="t.id" class="pl-card pl-tool quiet" :class="{on:drawerTool===t.id}" @click="openTool(t.id)">
                    <span class="pl-tool-b"><b>{{t.title}}</b><span class="pl-meta">{{toolPrice(t.e)}}</span></span>
                  </button>
                </template>
              </div>
            </div>
          </section>
          <ToolDrawer v-if="drawerEp" />

          <!-- A pasted key grants whatever the key itself can do: there is nothing to choose, so no section. -->
          <section class="pl-sec" v-if="(mkProvider.permission_capabilities||mkProvider.capabilities||[]).length">
            <h2 class="pl-h"><span>Permissions</span><i></i></h2>
            <p class="cat-hint">What {{mkProvider.display_name}} is asked to grant. Hover a line for the exact scope.</p>
            <div class="mk-perms">
              <div v-for="cap in (mkProvider.permission_capabilities||mkProvider.capabilities)" :key="cap" class="mk-perm">
                <div class="mk-perm-h">
                  <b>{{mkCapabilityLabel(cap)}}</b>
                  <span v-if="mkGranted.has(cap)" class="chip ok">granted</span>
                  <span v-else class="mk-quiet">not requested yet</span>
                </div>
                <p v-if="mkCapabilityIntro(cap)" class="mk-quiet" style="margin:8px 0 0">{{mkCapabilityIntro(cap)}}</p>
                <ul class="mk-perm-l">
                  <li v-for="d in mkCapabilityDetails(cap)" :key="d.scope||d.label" :title="d.scope||null">
                    <span class="mk-tick">✓</span>{{d.label}}
                  </li>
                </ul>
              </div>
            </div>
          </section>
</div>
</template>
