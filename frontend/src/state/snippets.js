
export default {
samplePath(host){ // known-good, runnable GET paths (relative to each tool's base_url) for common hosts
      return ({'api.trykitt.ai':'credit','httpbin.org':'headers','postman-echo.com':'get',
        'api.intercom.io':'me','api.stripe.com':'balance','api.render.com':'services?limit=1',
        'api.vercel.com':'v2/user','app.posthog.com':'api/projects/@current','eu.posthog.com':'api/projects/@current',
        'searchconsole.googleapis.com':'webmasters/v3/sites','googleads.googleapis.com':'v25/customers:listAccessibleCustomers',
        'tagmanager.googleapis.com':'tagmanager/v2/accounts',
        // part=snippet rather than the probe's part=id: same 1 quota unit, but it returns the channel
        // title, so the panel shows something a human recognises instead of an opaque UC… id.
        'youtube.googleapis.com':'youtube/v3/channels?part=snippet&mine=true',
        // identity endpoints: cheap, and they return a handle/name a human recognises
        'api.x.com':'2/users/me','api.linkedin.com':'v2/userinfo',
        // Both Meta providers share this host, so one entry covers Facebook Pages and Instagram.
        // /me is the person behind the token and needs no scope, so it prefills usefully whichever
        // asset the connection ended up bound to.
        'graph.facebook.com':'me?fields=id,name',
        'mybusinessaccountmanagement.googleapis.com':'v1/accounts','slack.com':'auth.test'})[host]||''; },
openCopy(t){ this.copyTool=t; this.snippetTab='cURL'; this.copied=false;
      // Prefer a real, runnable sample path so a copied snippet returns a result: a declared example,
      // else the health-check path, else a known-good sample for a well-known host, else the placeholder.
      const ex=(t.examples||[])[0], hp=t.health_check&&t.health_check.path;
      this.exPath=ex?ex.path:(hp||this.samplePath(t.host)||'<PATH>'); this.exMethod=ex?(ex.method||'GET'):'GET'; },
pickEx(ex){ this.exPath=ex.path; this.exMethod=ex.method||'GET'; },
buildSnippet(tab){
      const t=this.copyTool; if(!t) return {html:'',text:''};
      const proxy=(this.proxy||location.origin).replace(/\/$/,''); const up=t.base_url.replace(/\/$/,'');
      const path=this.exPath||'<PATH>'; const m=this.exMethod||'GET';
      // Escape before wrapping - the html goes through v-html, so a literal "<PATH>" (or any '<' in a
      // path/name) would otherwise be parsed as an (empty) element and vanish from the preview.
      const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
      const P=s=>`<span class="s-proxy">${esc(s)}</span>`, H=s=>`<span class="s-host">${esc(s)}</span>`,
            A=s=>`<span class="s-path">${esc(s)}</span>`, T=s=>`<span class="s-token">${esc(s)}</span>`,
            C=s=>`<span class="s-cmt">${esc(s)}</span>`, K=s=>`<span class="s-kw">${esc(s)}</span>`;
      const urlT=`${proxy}/call/${up}/${path}`;
      const urlH=`${P(proxy)}${C('/call/')}${H(up)}/${A(path)}`;
      const hint=(t.examples||[]).length?`e.g. ${t.examples[0].path}`:'the part after the domain';
      const tok=this.myToken||'$TREG_TOKEN', org=(this.activeOrg&&this.activeOrg.slug)||'$TREG_ORG';  // real token+org so a copied snippet runs as-is
      const tokShort=tok;  // show the full real token — the preview must match what Copy puts on the clipboard
      switch(tab){
        case 'Claude Code': return {
          text:`# Call "${t.name}" through treg - your key is injected server-side.\n${m} ${urlT}\nX-Treg-Token: ${tok}\nX-Treg-Org: ${org}\n\n# PATH = the ${t.host} path you'd normally call (${hint}).`,
          html:`${C('# Call "'+t.name+'" through treg - your key is injected server-side.')}\n${K(m)} ${urlH}\n${C('X-Treg-Token:')} ${T(tokShort)}\n${C('X-Treg-Org:')} ${T(org)}\n\n${C('# PATH = the '+t.host+" path you'd normally call ("+hint+').')}` };
        case 'CLI': return { text:`treg call ${t.name} ${path}${m!=='GET'?' --method '+m:''}`, html:`${P('treg call')} ${H(t.name)} ${A(path)}${m!=='GET'?' '+K('--method '+m):''}` };
        case 'Python': return {
          text:`import httpx\nr = httpx.request("${m}", "${urlT}", headers={"X-Treg-Token": "${tok}", "X-Treg-Org": "${org}"})\nprint(r.json())`,
          html:`import httpx\nr = httpx.request(${A('"'+m+'"')}, "${urlH}", headers={"X-Treg-Token": ${T('"'+tokShort+'"')}, "X-Treg-Org": ${T('"'+org+'"')}})\nprint(r.json())` };
        case 'Node': return {
          text:`const r = await fetch("${urlT}", {\n  method: "${m}",\n  headers: { "X-Treg-Token": "${tok}", "X-Treg-Org": "${org}" },\n});\nconsole.log(await r.json());`,
          html:`const r = await fetch("${urlH}", {\n  method: ${A('"'+m+'"')},\n  headers: { "X-Treg-Token": ${T('"'+tokShort+'"')}, "X-Treg-Org": ${T('"'+org+'"')} },\n});\nconsole.log(await r.json());` };
        case 'cURL': return {
          text:`TREG_API_TOKEN=${tok}\ncurl -X ${m} \\\n  -H "X-Treg-Token: $TREG_API_TOKEN" \\\n  -H "X-Treg-Org: ${org}" \\\n  "${urlT}"`,
          html:`TREG_API_TOKEN=${T(tokShort)}\n${K('curl')} -X ${K(m)} \\\n  -H "X-Treg-Token: ${T('$TREG_API_TOKEN')}" \\\n  -H "X-Treg-Org: ${T(org)}" \\\n  "${urlH}"` };
      }
    },
async copy(text){ if(!(await this.toClipboard(text))) return; this.copied=true; setTimeout(()=>this.copied=false,1500); }
}
