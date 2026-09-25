<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard, mounted(){ this.loadPlatforms() } }  // the catalog size in the copy
</script>

<template>

          <div class="rd-start">
            <div class="rd-welcome">
              <div class="rd-welcome-title"><span><img src="/media/redesign/welcome-mark.svg" alt=""></span><h1>Welcome! Getting started</h1></div>
              <button v-if="canRegister" class="rd-search" @click="go('connections'); $nextTick(()=>elements.search?.focus())"><img src="/media/redesign/search.svg" alt="">Search tools…</button>
            </div>
            <div class="start-card rd-setup">
              <div class="rd-setup-side">
                <div class="start-hd">
                  <span class="start-num">1</span><b>Set up your</b>
                  <div class="rd-agent-picker" @keydown.esc.stop="startAgentOpen=false; elements.startAgentTrigger.focus()" @focusout="!$event.currentTarget.contains($event.relatedTarget) && (startAgentOpen=false)">
                    <button :ref="el => setElement('startAgentTrigger', el)" type="button" class="agent-card" :aria-expanded="startAgentOpen" aria-controls="rd-agent-options" @click="startAgentOpen=!startAgentOpen">
                      <img v-if="welcomeAgent.icon" :src="welcome.agent==='openclaw'?'/media/redesign/openclaw.svg':agentIcon(welcomeAgent.icon)" alt=""><span>{{welcomeAgent.name}}</span><span aria-hidden="true">▾</span>
                    </button>
                    <div v-if="startAgentOpen" id="rd-agent-options" class="agent-menu">
                      <button v-for="a in welcomeAgents.concat(welcomeMoreAgents)" :key="a.id" type="button" class="agent-card" :class="{on:welcome.agent===a.id}" :aria-pressed="welcome.agent===a.id" @click="welcome.agent=a.id; startAgentOpen=false; elements.startAgentTrigger.focus()"><img v-if="a.icon" :src="a.id==='openclaw'?'/media/redesign/openclaw.svg':agentIcon(a.icon)" alt=""><span>{{a.name}}</span></button>
                    </div>
                  </div>
                </div>
                <img v-if="welcome.agent==='openclaw'" class="rd-agent-preview" src="/media/redesign/openclaw-preview.png" alt="OpenClaw">
                <div v-else class="rd-agent-preview rd-agent-fallback"><img v-if="welcomeAgent.icon" :src="agentIcon(welcomeAgent.icon)" alt=""><span>{{welcomeAgent.name}}</span></div>
              </div>
              <div class="start-bd rd-setup-body">
                <p class="rd-agent-context">Setting up treg for <b>{{welcomeAgent.name}}</b></p>
                <template v-if="welcomeAgent.plugin"><p>First, install the treg plugin:</p><a class="btn" :href="welcomeAgent.plugin" target="_blank" rel="noopener" @click="track('onboarding_plugin_install_clicked',{agent:welcome.agent,from:'start'})">Install plugin in {{welcomeAgent.name}} ↗</a></template>
                <section class="rd-setup-panel">
                  <p class="rd-panel-label">{{welcomeAgent.plugin ? "Then, in your Bot's chat, send:" : "In your agent's chat, send:"}}</p>
                  <div class="lc-codewrap"><button class="lc-cp" @click="copyStart(welcomeSetupCmd,'gsc')">{{startCopied==='gsc'?'Copied':'Copy'}}</button><pre>{{welcomeSetupCmd}}</pre></div>
                </section>
                <section v-if="myToken" class="rd-setup-panel">
                  <p class="rd-panel-label">Your API key</p>
                  <p class="rd-token-help">Setup signs your agent in automatically. This is your team's Default key (header <code>X-Treg-Token</code>). Additional and agent keys are random secrets shown only once; manage them in Team.</p>
                  <div class="lc-codewrap rd-token-code"><div class="rd-token-actions"><button class="rd-token-toggle" @click="startTokenShow=!startTokenShow" :aria-label="startTokenShow?'Hide key':'Show key'"><img :src="'/media/redesign/'+(startTokenShow?'eye-open':'eye-closed')+'.svg'" alt=""></button><button class="lc-cp" @click="copyStart(myToken,'gsk')">{{startCopied==='gsk'?'Copied':'Copy'}}</button></div><pre>{{startTokenShow ? myToken : (myToken.slice(0,14)+'••••••••••••••••')}}</pre></div>
                </section>
                <section v-else-if="defaultKeyState==='disabled'" class="rd-setup-panel">
                  <p class="rd-panel-label">Your API key</p>
                  <p class="rd-token-help">This team's Default key is disabled, so it is hidden and cannot be used.</p>
                  <button class="btn sm" @click="enableDefaultKey" :disabled="keyBusy">{{keyBusy?'…':'Enable key'}}</button>
                </section>

              </div>
            </div>

          <!-- ② Try it out -->
          <div class="start-card rd-try">
            <div class="start-hd"><span class="start-num">2</span><b style="font-size:16px">Try it out</b></div>
            <div class="start-bd">
              <p class="rd-try-intro">Copy an example below and send it to your agent.</p>
              <div class="try-grid">
                <button v-for="ex in tryExamples" :key="ex.k" type="button" class="try-card" :class="'rd-task-'+ex.k" @click="track('tryit_prompt_copied',{key:ex.k,cat:ex.cat,from:'getting_started'}); copyStart(ex.prompt,'try-'+ex.k)">
                  <span class="try-cat"><span style="display:inline-flex;align-items:center;gap:7px"><img class="try-ico" :src="ex.k==='ugc' ? '/logos/platforms/seedance.svg' : '/media/redesign/try-'+({trend:'tiktok',enr:'people',serp:'google',soc:'linkedin',posts:'linkedin'}[ex.k] || 'people')+'.svg'" alt=""/>{{ex.cat}}</span><span class="try-copy" :class="{done:startCopied==='try-'+ex.k}">{{startCopied==='try-'+ex.k ? '✓ copied' : '⧉ copy'}}</span></span>
                  <span class="try-txt">{{ex.show || ex.prompt}}</span>
                </button>
              </div>
              <div class="oauth-div"><span>also connect OAuth to unlock new agent capabilities</span></div>
              <div v-for="g in tryOauth" :key="g.label" style="margin-top:14px">
                <div class="oauth-grp">{{g.label}}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
                  <button v-for="p in g.items" :key="p.s" type="button" class="prov-chip" @click="track('tryit_oauth_clicked',{service:p.s,group:g.label,from:'getting_started'}); openProvider(p.s)"><img :src="'/media/redesign/oauth-'+({'google-business-profile':'business-profile','google-search-console':'search-console'}[p.s]||p.s)+'.svg'" alt=""/>{{p.n}}</button>
                  <span v-if="g.soon.length" class="soon-note" :data-tip="g.soon.map(p=>p.n).join(', ')">{{g.soon.length}} coming soon</span>
                </div>
              </div>
            </div>
          </div>

          <!-- BUILD ON TREG — deliberately NOT a numbered step. Steps 1-2 are the one path every
               new user walks; these two are optional and belong to different people (a vendor with an
               API to list, a platform embedding treg). Numbering them implied everyone had three
               things to do. Tabs, because nobody needs both. -->
          <div class="rd-build">
            <h2 style="margin:0 0 4px;font-size:17px">Build on treg</h2>
            <p class="sub" style="margin:0 0 12px">Optional, for two other kinds of user. Each is a skill file — paste the line into your coding agent, in your own repo, and it implements the rest.</p>
            <div class="seg">
              <button :class="{on:buildTab==='vendor'}" @click="buildTab='vendor'">List as vendor</button>
              <button :class="{on:buildTab==='platform'}" @click="buildTab='platform'">Integrate into your platform</button>
            </div>

            <div v-show="buildTab==='vendor'" class="rd-build-panel">
              <p class="sub">Put your API in front of every treg agent. Your agent reads the guide, writes the catalog entry for your endpoints and prices, and opens the PR.</p>
              <div class="lc-codewrap" style="margin-top:8px">
                <button class="lc-cp" @click="copyStart('Read '+proxy+'/vendor-listing.md and add our API to the treg catalog, then open a PR.','vend')">{{startCopied==='vend'?'✓ copied':'copy'}}</button>
                <pre>Read <span class="hl-str">{{proxy}}/vendor-listing.md</span> and add our API to the
treg catalog, then open a PR.</pre>
              </div>
            </div>

            <div v-show="buildTab==='platform'" class="rd-build-panel">
              <p class="sub">Give <i>your</i> users {{toolCountText||'thousands of'}} tools without owning the keys — and bill each of your customers for what they used. Covers the call methods (HTTP, MCP, CLI), per-customer tagging, spend limits and invoicing.</p>
              <div class="lc-codewrap" style="margin-top:8px">
                <button class="lc-cp" @click="copyStart('Read '+proxy+'/integrate.md and integrate treg into our product, including per-customer usage tracking and billing.','intg')">{{startCopied==='intg'?'✓ copied':'copy'}}</button>
                <pre>Read <span class="hl-str">{{proxy}}/integrate.md</span> and integrate treg into our
product, including per-customer usage tracking and billing.</pre>
              </div>
            </div>
          </div>

          <!-- The manual CLI paths, kept but tucked away -->
          <details style="margin-top:26px" :open="startTab==='setup'">
            <summary class="sub" style="cursor:pointer;margin-bottom:0">Prefer the terminal? Manual CLI setup &amp; sharing your team's keys →</summary>
            <div class="seg" style="margin-top:14px">
            <button :class="{on:startTab==='access'}" @click="startTab='access'">Access 2000+ treg tools</button>
            <button :class="{on:startTab==='setup'}" @click="startTab='setup'">Set up your own tools</button>
          </div>

          <!-- TAB · Access the catalog — agent instruction first, then the manual CLI walkthrough -->
          <div v-show="startTab==='access'" style="max-width:720px">
            <p class="sub">{{toolCountText ? toolCountText+' catalogued endpoints' : 'Catalogued endpoints'}}: SEO and backlinks, social and trends, people and company enrichment, ads. Find one by what it <i>does</i>, see its price, call it — no provider signup. New verified accounts get <b>$1.00 free credit once</b> on an eligible team, covering hundreds of calls.</p>

            <div class="lbl" style="margin-top:16px">▸ Set up with your agent <span class="muted" style="font-weight:400">— paste &amp; go</span></div>
            <p class="sub">One line, token included — your agent reads llms.txt and does the rest: installs the CLI, signs in as you, and makes its first call.</p>
            <div class="lc-codewrap" style="margin-top:8px"><button class="lc-cp" @click="copyAgentGuide('agent')">{{agentRowCopied==='agent'?'✓ copied':'copy'}}</button><pre style="max-height:240px;overflow:auto">{{buildAgentPrompt('agent', true)}}</pre></div>

            <div class="lbl" style="margin-top:26px">▸ Or set it up manually</div>
            <div class="lbl" style="margin-top:14px">1 · Install the CLI</div>
            <p class="sub">Installs the <span class="mono">treg</span> command and points it at this server. Upgrade later with <span class="mono">treg update</span>.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('curl -fsSL '+proxy+'/install.sh | sh','i')">{{startCopied==='i'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">curl</span> -fsSL <span class="hl-str">{{proxy}}/install.sh</span> | sh</pre></div>

            <div class="lbl" style="margin-top:22px">2 · Sign in</div>
            <p class="sub">Opens your browser — GitHub, Google, or an email code. Your first sign-in also registers you.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg login','l')">{{startCopied==='l'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">treg</span> login</pre></div>

            <div class="lbl" style="margin-top:22px">3 · Call something — no key, nothing to set up</div>
            <p class="sub">Search the catalog by what you need done, check the price, call it, and see exactly what it cost.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg catalog search &quot;backlinks for a domain&quot;\ntreg call tikhub.tiktok.user.profile --query uniqueId=tiktok\ntreg balance','cat1')">{{startCopied==='cat1'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">treg</span> catalog search <span class="hl-str">"backlinks for a domain"</span>
<span class="hl-cmd">treg</span> call tikhub.tiktok.user.profile <span class="hl-flag">--query</span> uniqueId=tiktok
<span class="hl-cmd">treg</span> balance      <span class="hl-comment"># see exactly what it cost</span></pre></div>
          </div>

          <!-- TAB · Setup the team vault — two sections: set it up, then let the team use it -->
          <div v-show="startTab==='setup'" style="max-width:720px">
            <p class="sub">Share your skills &amp; API keys with the team — your <span class="mono">.env</span> keys and skill folders become tools every teammate's agent can call, without the key ever leaving the server.</p>

            <div class="lbl" style="margin-top:16px">▸ Set up with your agent <span class="muted" style="font-weight:400">— paste &amp; go</span></div>
            <p class="sub">One line, token included — your agent reads llms.txt, installs the CLI, signs in as you, and can register your local skills + keys as shared tools (read-only scan first, you approve).</p>
            <div class="lc-codewrap" style="margin-top:8px"><button class="lc-cp" @click="copyAgentGuide('agent')">{{agentRowCopied==='agent'?'✓ copied':'copy'}}</button><pre style="max-height:240px;overflow:auto">{{buildAgentPrompt('agent', true)}}</pre></div>

            <div class="lbl" style="margin-top:26px">▸ Or set it up manually</div>
            <div class="lbl" style="margin-top:14px">1 · Install the CLI</div>
            <p class="sub">Installs the <span class="mono">treg</span> command and points it at this server. Upgrade later with <span class="mono">treg update</span>.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('curl -fsSL '+proxy+'/install.sh | sh','si')">{{startCopied==='si'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">curl</span> -fsSL <span class="hl-str">{{proxy}}/install.sh</span> | sh</pre></div>

            <div class="lbl" style="margin-top:22px">2 · Sign in</div>
            <p class="sub">Opens your browser — GitHub, Google, or an email code. Your first sign-in also registers you.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg login','sl')">{{startCopied==='sl'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">treg</span> login</pre></div>

            <div class="lbl" style="margin-top:22px">3 · Upload your keys &amp; skills</div>
            <p class="sub">Two ways to give your agents power: share a whole <b>skill</b>, or register a single <b>API endpoint</b>. Preview with <span class="mono">treg scan</span> first. Idempotent — re-run freely.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg upload                 # both .env + skills in this dir\ntreg upload env --select openai,stripe\ntreg upload skills --dir ~/.claude/skills --all      # register skill from a specific path','im')">{{startCopied==='im'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">treg</span> upload                 <span class="hl-comment"># both .env + skills in this dir</span>
<span class="hl-cmd">treg</span> upload env <span class="hl-flag">--select</span> openai,stripe
<span class="hl-cmd">treg</span> upload skills <span class="hl-flag">--dir</span> <span class="hl-str">~/.claude/skills</span> <span class="hl-flag">--all</span>      <span class="hl-comment"># register skill from a specific path</span></pre></div>

            <div class="lbl" style="margin-top:30px">▸ Then — let the team use it</div>
            <p class="sub">Your team's own tools: a teammate pulls a shared skill and calls any registered API, with no keys on their machine. These are never metered — your team already pays for them.</p>

            <div class="lbl" style="margin-top:14px">4 · Use the team's shared tools</div>
            <p class="sub">Browse the shared library, pull a skill, and call a tool — the credential stays on the server, never on your machine.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg skill ls\ntreg skill install seo-blog-writer                    # a teammate pulls one (or --all)\ntreg call https://api.stripe.com/v1/charges','cc')">{{startCopied==='cc'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">treg</span> skill ls
<span class="hl-cmd">treg</span> skill install seo-blog-writer
<span class="hl-cmd">treg</span> call <span class="hl-str">https://api.stripe.com/v1/charges</span></pre></div>
          </div>
          </details>

          </div>

</template>
