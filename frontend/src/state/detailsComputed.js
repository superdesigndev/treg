export default {
detailShareUrl(){ if(!this.detail) return ''; return location.origin+'/app/'+(this.detail.kind==='skill'?'skills':'tools')+'/'+encodeURIComponent(this.detail.name); },
shareInviteUrl(){  // the DM-able one-click link: page URL + the invited email (prefills sign-in; the
      // invite then auto-accepts on landing-match). Deliberately NOT the emailed token — that secret can
      // mint a session and must never leave the inbox; this link still requires proving the email.
      return this.share.sent ? this.detailShareUrl+'?invite='+encodeURIComponent(this.share.sent.email) : ''; },
detailTree(){  // the bundle's files as a flat, indent-rendered tree: synthesized dir rows + file rows
      const files=Object.keys((this.detailData&&this.detailData.files)||{}).sort();
      const rows=[{path:'SKILL.md', name:'SKILL.md', depth:0, dir:false}]; const seen=new Set();
      for(const p of files){ const parts=p.split('/');
        for(let i=0;i<parts.length-1;i++){ const d=parts.slice(0,i+1).join('/');
          if(!seen.has(d)){ seen.add(d); rows.push({path:d, name:parts[i]+'/', depth:i, dir:true}); } }
        rows.push({path:p, name:parts[parts.length-1], depth:parts.length-1, dir:false}); }
      return rows; },
detailFileContent(){ if(!this.detailData) return '';
      if(this.detailFile==='SKILL.md') return this.detailData.recipe||'';
      return (this.detailData.files||{})[this.detailFile]||''; },
detailParentSkill(){ if(!this.detail||this.detail.kind!=='tool'||!this.detailData||!this.detailData.bundle_id) return null;
      return this.bundles.find(b=>b.id===this.detailData.bundle_id)||null; },
detailPrompt(){  // the copyable "give this to your agent" instruction — no token embedded, the
      // recipient signs in as themselves (this text is meant to be forwarded)
      if(!this.detail) return '';
      const B=(this.proxy||location.origin).replace(/\/$/,''); const n=this.detail.name;
      const head=[`I want to use the shared ${this.detail.kind==='skill'?'skill':'tool'} "${n}" from my team's treg at ${B} .`,``,
        `1. Install the treg CLI (skip if \`treg\` already works) and sign me in:`,
        `   curl -fsSL ${B}/install.sh | sh`,`   treg login`,``];
      if(this.detail.kind==='skill'){
        const t=(this.detailData&&this.detailData.tools&&this.detailData.tools[0])||null;
        return head.concat([
          `2. Install the skill into this project (writes the skill folder into my agent skills dir):`,
          `   treg skill install ${n}`,``,
          ...(t?[`3. The skill's API calls go through treg's proxy — the credential is injected server-side, never on this machine:`,
                `   treg call ${t.name} <PATH>   # or prefix the real URL: ${B}/call/<full upstream URL>`,``]:[]),
          `Then use the "${n}" skill for my request.`,``,
          `Full protocol reference: ${B}/llms.txt`]).join('\n');
      }
      const t=this.detailData||{};
      return head.concat([
        `2. Call it through the proxy — the credential is injected server-side, never on this machine:`,
        `   treg call ${n} <PATH>   # PATH = the ${t.host||'upstream'} path you'd normally call`,
        ...(t.cli?[``,`   # it's also a CLI — run it with the key injected:`,`   treg run ${n} -- <cli args>`]:[]),``,
        `Full protocol reference: ${B}/llms.txt`]).join('\n');
    }
}
