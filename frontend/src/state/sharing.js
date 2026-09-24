
export default {
copyMd(url){ fetch(url).then(r=>r.text()).then(t=>navigator.clipboard.writeText(t)).catch(()=>{}); },
// copy raw markdown
    openMd(url){ window.open(url,'_blank'); },
// open the .md in a new tab (method, not a template global)
    copyLlms(){ navigator.clipboard.writeText(this.proxy+'/llms.txt').then(()=>{ this.llmsCopied=true; setTimeout(()=>{ this.llmsCopied=false; },1500); }).catch(()=>{}); },
async copyToken(){ if(!this.myToken) return; if(!(await this.toClipboard(this.myToken))) return; this.tokenCopied=true; setTimeout(()=>{ this.tokenCopied=false; },1500); },
// ONE instruction, not two. The old pair (admin "Setup" / consumer "Connect") each re-explained
    // the catalog, the credential ladder, prices and the balance — knowledge that now lives in the
    // skill install.sh installs, and in llms.txt. A prompt can only carry what a FILE cannot: the
    // token, the team, permission to run things, and "do it now". `kind` is ignored (call sites keep
    // their argument) so there is a single text to keep true.
    buildAgentPrompt(kind, inclToken){  // ONE line — llms.txt carries the whole setup flow (auth included)
      return TregAgentSetup.command(this.proxy);
    },
async copyAgentPrompt(){ if(!(await this.toClipboard(this.agentPromptText))) return; this.agentGuideCopied=true; setTimeout(()=>this.agentGuideCopied=false,1500); },
async copyVendorPrompt(){ if(!(await this.toClipboard(this.vendorPromptText))) return; this.vendorCopied=true; setTimeout(()=>this.vendorCopied=false,1500); },
openToolRequest(){ this.reqDone=false; this.reqErr=''; this.reqForm.capability=(this.q||'').trim(); this.reqAsk=true; },
// pre-fill with the search that came up short
    async submitToolRequest(){ const cap=(this.reqForm.capability||'').trim();
      if(!cap){ this.reqErr='Say what tool or capability you need.'; return; }
      this.reqBusy=true; this.reqErr='';
      try{ await this.api('/tool-requests',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({capability:cap, query:(this.q||'').trim(), note:this.reqForm.note, contact:this.reqForm.contact, source:'web'})});
        this.reqDone=true; this.reqForm={capability:'', note:'', contact:''}; }
      catch(e){ this.reqErr = e.status===429?'Too many requests from here — try again later.':('Could not send: '+(e.detail||e.status)); }
      finally{ this.reqBusy=false; } },
async copyAgentGuide(kind){ if(!(await this.toClipboard(this.buildAgentPrompt(kind, true)))) return; this.agentRowCopied=kind; setTimeout(()=>{ if(this.agentRowCopied===kind) this.agentRowCopied=null; },1500); }
}
