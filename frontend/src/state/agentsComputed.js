export default {
snip(){ return this.buildSnippet(this.snippetTab); },
recipeSnip(){ return this.recipeSnippet(this.recipeTab); },
tutData(){ return window.TREG_TUTORIAL || {concepts:[], roles:{cols:[],rows:[]}, steps:[]}; },
tutSteps(){ return this.tutData.steps; },
tutStep(){ return this.tutSteps[this.tut.i]; },
xtutSteps(){ return (this.helpMode==='import-shell' ? this.tutData.importShell : this.tutData.access) || []; },
xtutStep(){ return this.xtutSteps[this.xtut.i]; },
xtutTitle(){ return this.helpMode==='import-shell' ? 'Import & shell' : 'Team access control'; },
tourData(){ return window.TREG_TOUR || {steps:[], personas:{}, colors:['--accent']}; },
tourSteps(){ return this.tourData.steps; },
tourStep(){ return this.tourSteps[this.tourI]; },
tourParts(){ return [...new Set(this.tourSteps.map(s=>s.part))]; },
agentPromptText(){ return this.buildAgentPrompt(this.agentGuide); },
// The whole vendor pitch is two sentences: the hosted /vendor-listing page carries the real
    // instructions, so this prompt only has to name the repo, the page, and the contact email.
    vendorPromptText(){ return `Help me create a PR to treg (https://github.com/superdesigndev/treg) that adds our API to its tool catalog. Follow the instructions at ${this.proxy}/vendor-listing — and include our contact email in the PR description so the maintainers can reach us to arrange live verification.`; },
// What to actually DO with a freshly minted agent token, in the three shapes people need.
    agentSnippet(){ const a=this.newAgent||this.snipAgent||((this.newApiKey&&this.newApiKey.assigned_type==='agent')?this.newApiKey:null); if(!a) return '';
      const base=this.proxy||location.origin, org=a.org||this.activeSlugNow;
      const tok=a.token||a.secret||this.agentTokens[a.user_id]||'$TREG_TOKEN';  // hash-stored server-side; real value only if minted this page-load
      if(this.agentSnip==='env'){
        return [
          '# Where to persist these, per agent harness:',
          '#  Claude Code   -> the "env" map in ~/.claude/settings.json',
          '#  Codex CLI     -> [shell_environment_policy] set = {...} in ~/.codex/config.toml',
          '#  Gemini CLI    -> append KEY=value lines to ~/.gemini/.env (a project .env shadows it)',
          '#  Cursor        -> ~/.zshrc: [ -n "$CURSOR_AGENT" ] && export ...',
          '#  pi            -> ~/.zshrc guard on $PI_CODING_AGENT, or shellCommandPrefix in ~/.pi/agent/settings.json',
          '#  opencode      -> shell.env plugin; also export TREG_CLIENT=opencode',
          '#  plain shell / CI -> the exports below, as-is',
          'export TREG_TOKEN='+tok,
          'export TREG_ORG='+org,
          'export TREG_URL='+base,
        ].join('\n'); }
      return [
        'You have your own treg identity in the team "'+org+'" (you are "'+a.name+'").',
        '',
        'FIRST, persist this identity into YOUR OWN global config, so every future session of',
        'yours, in any repo, runs as "'+a.name+'" — while other agents and the human keep theirs:',
        '  TREG_TOKEN='+tok,
        '  TREG_ORG='+org,
        '  TREG_URL='+base,
        '',
        'How, per runtime — do the one that matches you:',
        '- Claude Code: add them to the "env" map in ~/.claude/settings.json:',
        '    "env": { "TREG_TOKEN": "'+tok+'", "TREG_ORG": "'+org+'", "TREG_URL": "'+base+'" }',
        '- Codex CLI: in ~/.codex/config.toml (a `set` entry bypasses Codex\'s default filtering',
        '  of *TOKEN* names, so this works where plain shell exports get stripped):',
        '    [shell_environment_policy]',
        '    set = { TREG_TOKEN = "'+tok+'", TREG_ORG = "'+org+'", TREG_URL = "'+base+'" }',
        '- Gemini CLI: append the three KEY=value lines to ~/.gemini/.env. Caveat: a project',
        '  .env shadows that file entirely, so repos with their own .env must carry them too.',
        '- Cursor: a guarded line in the shell profile (~/.zshrc or ~/.bashrc):',
        '    [ -n "$CURSOR_AGENT" ] && export TREG_TOKEN='+tok+' TREG_ORG='+org+' TREG_URL='+base,
        '- pi: same guarded profile line keyed on $PI_CODING_AGENT — or set',
        '  "shellCommandPrefix": "source ~/.pi-env.sh" in ~/.pi/agent/settings.json and put the',
        '  exports in that file.',
        '- opencode: no global env config and no fingerprint var — use a shell.env plugin, or an',
        '  unguarded profile export ONLY on a machine that runs no other agents; ALSO export',
        '  TREG_CLIENT=opencode so your calls are attributed to your runtime.',
        '- anything else: a profile line guarded by an env var only your runtime sets, plus',
        '  TREG_CLIENT=<your-runtime-name> if treg does not already detect you.',
        '',
        'Do NOT put the token in a repo file (.env, settings committed to git) — it is a secret,',
        'and a repo-level file would also hand YOUR identity to every other agent in that repo.',
        '',
        'From then on the treg CLI automatically acts as you (TREG_TOKEN beats the machine',
        'owner\'s ~/.treg config), and raw HTTP works anywhere:',
        '  curl '+base+'/call/<tool>/<path> -H "X-Treg-Token: $TREG_TOKEN" -H "X-Treg-Org: '+org+'"',
        '',
        'List what you may use:  GET '+base+'/tools',
        'Your calls are capped and logged as "'+a.name+'", so keep them purposeful.',
        '',
        'FINALLY, after persisting the env, confirm the identity landed by checking in AS yourself:',
        '  curl -s -X POST '+base+'/agents/checkin -H "X-Treg-Token: '+tok+'" -H "X-Treg-Org: '+org+'"',
        'A {"connected": true, "you": "agent-…"} reply means treg registered the connection —',
        'the dashboard shows this agent as connected from that moment.',
      ].join('\n'); },
rosterMembers(){  // one roster: each person, then the agents they minted, then the runtimes
      // DETECTED in their traffic (observed — not yet their own identity, hence "Scope this agent").
      // Orphaned agents (creator left, or pre-created_by rows) trail at the end.
      const humans=(this.orgMembers||[]).filter(m=>!m.is_agent);
      const agents=(this.orgMembers||[]).filter(m=>m.is_agent);
      const obs=(this.observedAgents||[]);
      const out=[];
      humans.forEach(h=>{ out.push({...h, key:'u'+h.user_id});
        agents.filter(a=>a.created_by===h.email).forEach(a=>out.push({...a, key:'u'+a.user_id}));
        obs.filter(o=>o.member===h.email).forEach(o=>out.push({...o, is_observed:true, key:'o'+o.member+'/'+o.client})); });
      agents.filter(a=>!humans.some(h=>h.email===a.created_by)).forEach(a=>out.push({...a, key:'u'+a.user_id}));
      return out; },
agentConnected(){ const n=this.newAgent; if(!n) return false;
      const row=(this.agents||[]).find(a=>a.user_id===n.user_id); return !!(row&&row.connected); },
isOwner(){ return this.activeRole==='owner'; }
}
