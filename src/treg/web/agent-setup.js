/* Shared by the dashboard welcome flow and Enrich Arena. No credentials are stored here. */
(function(global){
  const agents=[
    {id:'openclaw',name:'OpenClaw',icon:'openclaw-color'},
    {id:'grokbot',name:'Grok Bot',icon:'/logos/agents/grokbot.png',plugin:'https://x.ai/bot/plugin/55647425'},
    {id:'hermes',name:'Hermes Agent',icon:'hermesagent'},
    {id:'claudeai',name:'Claude.ai',icon:'claude-color'},
    {id:'claude-code',name:'Claude Code',icon:'claudecode-color'},
    {id:'codex',name:'Codex',icon:'codex-color'},
  ];
  const moreAgents=[
    {id:'opencode',name:'opencode',icon:'opencode'},
    {id:'pi',name:'pi',icon:'pi'},
    {id:'cursor',name:'Cursor',icon:'cursor'},
    {id:'gemini-cli',name:'Gemini CLI',icon:'gemini-color'},
    {id:'other',name:'Other',icon:null},
  ];
  const iconUrl=(icon,theme='light')=>icon?.startsWith('/')?icon:icon?'https://unpkg.com/@lobehub/icons-static-png@latest/'+theme+'/'+icon+'.png':'';
  const command=base=>'set up treg — '+base.replace(/\/$/,'')+'/llms.txt';
  function setupText(command,team,token,masked=false){
    if(!team&&!token)return command;
    const value=token?(masked?token.slice(0,14)+'••••••••••••••••':token):'<YOUR_TOKEN>';
    return command+'\n\nwith team '+(team||'<team-slug>')+' token: '+value;
  }
  const AgentPicker={
    props:{modelValue:String,icon:{type:Function,default:iconUrl}},emits:['update:modelValue'],data:()=>({moreOpen:false}),
    computed:{agents:()=>agents,moreAgents:()=>moreAgents,moreSelected(){return moreAgents.find(a=>a.id===this.modelValue);}},
    methods:{pick(id){this.$emit('update:modelValue',id);this.moreOpen=false;}},
    template:`<div><div class="agent-grid">
      <button v-for="a in agents" :key="a.id" type="button" class="agent-card" :class="{on:modelValue===a.id}" :aria-pressed="modelValue===a.id" @click="pick(a.id)"><img :src="icon(a.icon)" alt="" @error="$event.target.style.visibility='hidden'"><span>{{a.name}}</span></button>
      <button type="button" class="agent-card" :class="{on:moreOpen||moreSelected}" :aria-expanded="moreOpen" @click="moreOpen=!moreOpen"><span aria-hidden="true">☰</span><span>{{moreSelected&&!moreOpen?moreSelected.name:'More'}}</span><span aria-hidden="true" style="margin-left:auto">▾</span></button>
    </div><div v-if="moreOpen" class="agent-grid" style="margin-top:10px">
      <button v-for="a in moreAgents" :key="a.id" type="button" class="agent-card" :class="{on:modelValue===a.id}" :aria-pressed="modelValue===a.id" @click="pick(a.id)"><img v-if="a.icon" :src="icon(a.icon)" alt="" @error="$event.target.style.visibility='hidden'"><span v-else aria-hidden="true">✳</span><span>{{a.name}}</span></button>
    </div></div>`
  };
  const SetupInstructions={
    props:{agent:Object,icon:{type:Function,default:iconUrl},command:String,team:String,token:String,showToken:Boolean,copied:Boolean,copyDisabled:Boolean},
    emits:['copy','toggle-token','plugin'],
    computed:{full(){return setupText(this.command,this.team,this.token);},masked(){return setupText(this.command,this.team,this.token,!this.showToken);}},
    template:`<div class="agent-setup-instructions">
      <p class="sub" style="display:flex;align-items:center;gap:8px;margin:0 0 16px"><img v-if="agent.icon" :src="icon(agent.icon)" alt="" style="width:18px;height:18px" @error="$event.target.style.visibility='hidden'">Setting up treg for <b style="color:var(--ink)">{{agent.name}}</b></p>
      <template v-if="agent.plugin"><h2 style="margin:0 0 10px;font-size:19px">1. Install the treg plugin</h2><a class="btn" :href="agent.plugin" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:8px;margin-bottom:18px" @click="$emit('plugin')"><img :src="icon(agent.icon)" alt="" style="width:16px;height:16px">Install plugin in {{agent.name}} ↗</a></template>
      <div class="lc-codewrap" style="padding:16px"><div class="setup-prompt-header" style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px"><h2 style="margin:0;font-size:14px;font-weight:500;line-height:1.5;color:inherit">{{agent.plugin ? "2. In your Bot's chat, send:" : "In your agent's chat, send:"}}</h2><button class="lc-cp" style="position:static;flex-shrink:0" type="button" :disabled="copyDisabled" @click="$emit('copy',full)">{{copied?'✓ copied':'Copy'}}</button></div><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{masked}}</pre></div>
      <button v-if="token" class="text-button" type="button" style="font-size:12px;margin-top:6px" @click="$emit('toggle-token')">{{showToken?'Hide':'Show'}} key</button>
      <p class="sub" style="font-size:12px;margin:12px 0 0">{{team&&token ? (agent.plugin?'Your Bot reads that file and signs in with your team & token — then it can call every tool in the catalog.':'Your agent reads that file and signs in with your team & token — it installs the CLI and starts calling tools.') : 'Your agent reads that file, installs the CLI and guides you through signing in.'}}</p>
    </div>`
  };
  const examples=[
      {k:'trend',cat:'Trending videos pattern', logo:'tiktok',  prompt:'Use treg to pull today\'s trending TikTok videos (video links included)'},
      {k:'enr',  cat:'Get contact emails',      logo:'people', avatar:'https://pbs.twimg.com/profile_images/1131851609774985216/OcsssQ9J_400x400.png', prompt:'Use treg to find the work email of Peter Steinberger'},
      {k:'serp', cat:'Keyword volume',          logo:'google',  prompt:'Use treg to pull real monthly search volume and top related keywords worth targeting for my business'},
      {k:'soc',  cat:'Scrape linkedin',         logo:'linkedin',prompt:'Use treg to look up linkedin.com/in/jasonzhoudesign'},
    ];
  const oauthGroups=[
      {label:'Post on social',      items:[{s:'x',n:'X (Twitter)'},{s:'youtube',n:'YouTube'},{s:'tiktok',n:'TikTok'},{s:'linkedin',n:'LinkedIn'},{s:'facebook',n:'Facebook Pages'},{s:'instagram',n:'Instagram'}],
                                    soon:[]},
      {label:'Manage ad campaigns', items:[{s:'google-ads',n:'Google Ads'},{s:'meta-ads',n:'Meta Ads'}], soon:[]},
      {label:'SEO on your own site',items:[{s:'google-analytics',n:'Google Analytics'},{s:'google-search-console',n:'Search Console'},{s:'google-business-profile',n:'Business Profile'}], soon:[]},
    ];
  const TryItOut={
    props:{copied:String,headingId:String},emits:['example','provider'],
    computed:{examples:()=>examples,oauthGroups:()=>oauthGroups},
    template:`<div>
            <h2 :id="headingId" style="margin:0 0 6px;font-size:19px">Try it out</h2>
            <div class="wc-wait" style="margin:0 0 16px"><span class="wc-waitdot"></span>Waiting for your agent — copy an example below and send it to get your first result.</div>
            <div class="try-grid">
              <button v-for="ex in examples" :key="ex.k" type="button" class="try-card" @click="$emit('example',ex)">
                <span class="try-cat"><span style="display:inline-flex;align-items:center;gap:7px"><img class="try-ico" :src="'/logos/platforms/'+ex.logo+'.svg'" alt=""/><img v-if="ex.avatar" class="try-ico avatar" :src="ex.avatar" alt=""/>{{ex.cat}}</span><span class="try-copy" :class="{done:copied===ex.k}">{{copied===ex.k ? '✓ copied' : '⧉ copy'}}</span></span>
                <span class="try-txt">{{ex.prompt}}</span>
              </button>
            </div>
            <div class="oauth-div"><span>also connect OAuth to unlock new agent capabilities</span></div>
            <div v-for="g in oauthGroups" :key="g.label" style="margin-top:12px">
              <div class="oauth-grp">{{g.label}}</div>
              <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
                <button v-for="p in g.items" :key="p.s" type="button" class="prov-chip" @click="$emit('provider',p.s,g.label)"><img :src="'/logos/'+p.s+'.svg'" alt=""/>{{p.n}}</button>
                <span v-if="g.soon.length" class="soon-note" :data-tip="g.soon.map(p=>p.n).join(', ')">{{g.soon.length}} coming soon</span>
              </div>
            </div>
</div>`
  };
  global.TregAgentSetup={agents,moreAgents,iconUrl,command,setupText,AgentPicker,SetupInstructions,examples,oauthGroups,TryItOut};
})(globalThis);
