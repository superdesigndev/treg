<script>
import { useDashboard } from '../state/context'
import OwnToolsHeader from '../components/OwnToolsHeader.vue'
import OwnToolCard from '../components/OwnToolCard.vue'
import FilterBox from '../components/FilterBox.vue'
import LoadFromMachine from '../components/LoadFromMachine.vue'
export default { components: { OwnToolsHeader, OwnToolCard, FilterBox, LoadFromMachine }, setup: useDashboard, mounted(){ this.loadPlatforms() } }  // the catalog size in the copy
</script>

<template>
<div class="pl own">
  <OwnToolsHeader current="tools">
    <template #actions v-if="canRegister">
      <button class="pl-btn ghost" @click="openAddSkill">Import skill</button>
      <span class="own-add">
        <button class="pl-btn" @click.stop="addToolMenu=!addToolMenu" aria-haspopup="true" :aria-expanded="addToolMenu">Add tool</button>
        <div v-if="addToolMenu" class="own-add-scrim" @click="addToolMenu=false"></div>
        <div class="dropdown own-add-menu" v-if="addToolMenu" @click.stop>
          <div class="row" @click="openAddTool('endpoint')"><span><b>Endpoint</b><span class="sub">an HTTP API, called through the proxy</span></span></div>
          <div class="row" @click="openAddTool('cli')"><span><b>CLI</b><span class="sub">a command members run with the key injected</span></span></div>
          <div class="row" @click="addToolMenu=false; go('catalog')"><span><b>From the catalog</b><span class="sub">ready-made endpoints by platform, many need no key</span></span></div>
        </div>
      </span>
    </template>
  </OwnToolsHeader>

  <div v-if="orgMsg" class="banner cn-banner"><span>{{orgMsg}}</span><button class="btn sm ico" @click="orgMsg=''" aria-label="Dismiss">✕</button></div>
  <div v-if="toolErr && !newTool" class="banner cn-banner"><span>{{toolErr}}</span></div>
  <div v-if="runNote" class="banner cn-banner">
    <span>Local runs are on for {{runNote}}. During a run the key is injected into that member's process - consider storing a <b>restricted</b> key (many providers offer read-only/scoped keys) rather than a full one.</span>
    <button class="btn sm ico" @click="runNote=''" aria-label="Dismiss">✕</button>
  </div>

  <div class="cn-filters" v-if="hasAnyTools || q">
    <FilterBox v-model="q" label="Search your own tools" placeholder="Search your own tools by name or host" :register="el => setElement('search', el)" />
    <div class="seg pl-seg" role="radiogroup" aria-label="Kind of tool">
      <button v-for="k in [{key:'all', label:'All', n:ownToolCount}, {key:'endpoints', label:'APIs & CLIs', n:endpoints.length},
                           {key:'skills', label:'Integration skills', n:skillTools.length}, {key:'recipes', label:'Skills', n:recipes.length}]"
              :key="k.key" role="radio" :aria-checked="toolTab===k.key" :class="{on:toolTab===k.key}" @click="toolTab=k.key">{{k.label}} <span>{{k.n}}</span></button>
    </div>
  </div>

  <section class="pl-sec" v-for="grp in toolGroups" :key="grp.key" v-show="grp.rows.length && (toolTab==='all'||toolTab===grp.key)">
    <h2 class="pl-h"><span>{{grp.label}}</span><i></i><em>{{grp.rows.length}}</em></h2>
    <p class="cat-hint">{{grp.hint}}</p>
    <div class="pl-grid pl-grid-t">
      <OwnToolCard v-for="t in grp.rows" :key="t.id" :t="t" />
    </div>
  </section>

  <section class="pl-sec" v-show="recipes.length && (toolTab==='all'||toolTab==='recipes')">
    <h2 class="pl-h"><span>Skills</span><i></i><em>{{recipes.length}}</em></h2>
    <p class="cat-hint">Knowledge skills. An agent installs one with the CLI.</p>
    <div class="pl-grid pl-grid-t">
      <OwnToolCard v-for="r in recipes" :key="r.id" :skill="r" />
    </div>
  </section>

  <p v-if="!loading && !hasAnyTools && (q || !canRegister || personalWithTeams)" class="pl-empty">Nothing here{{q?' matches "'+q+'"':' yet'}}.<template v-if="!q && personalWithTeams"> This is your <b>personal</b> space - your team's tools live under another org. <a href="#" @click.prevent="jumpToTeam()">Switch to a team →</a></template></p>
  <LoadFromMachine v-if="!loading && !hasAnyTools && !q && canRegister" :title="'Nothing of your own yet. You can already call '+(toolCountText||'thousands of')+' tools'">
    New verified accounts get <b>$1.00 of free credit once</b> when creating an eligible team, so the catalog works before you register anything: find a tool by what it does, see the price, call it. <a href="#" @click.prevent="go('catalog')">Browse the catalog →</a><br>When you're ready to add your <i>own</i> keys and skills (your <span class="mono">.env</span> and skill folders) load them here and they become callable tools for the whole team.
  </LoadFromMachine>
</div>
</template>
