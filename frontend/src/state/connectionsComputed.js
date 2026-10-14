// Connections: every account and key the team holds, and every provider one can be added for.
const byName = new Intl.Collator()
// Accounts that need a person come first, in the order a person should deal with them.
const RANK = {reconnect:0, second:1, failing:2, setup:3, choose:4, ok:5, paused:6}

export default {
providerIndex(){ return new Map(this.providers.map(p=>[p.service,p])); },
// A provider key saved as a secret NAMED for the provider (`treg secret add apollo …`, or a
    // Secrets row) rather than connected. The credential ladder uses it all the same, so it belongs
    // beside the connections, not among the team's own-tool secrets. A connected credential for the
    // same provider outranks it, and its card says so.
    namedKeys(){
      const ids=new Set(this.connections.map(c=>c.id));
      const tagged=new Set(this.connections.map(c=>c.provider));
      return this.secrets.flatMap(s=>{ const p=this.providerIndex.get(s.name);
        return !ids.has(s.id) && this.pastedCredential(p) ? [{s, p, shadowed:tagged.has(s.name)}] : []; });
    },
// What the Secrets page lists: the credentials the team's own tools use, and nothing Connections shows.
    ownSecrets(){
      const shown=new Set(this.connAccounts.map(a=>(a.c||a.s).id));
      return this.secrets.filter(s=>!shown.has(s.id));
    },
// Every credential the team holds for a catalog provider, as one kind of row whichever way it was
    // added: a connection (`c`) or a named key (`s`). `pasted` is computed once here because every
    // card asks it several times.
    connAccounts(){
      // A grant with no catalog provider (an own-app OAuth connect) has no provider page to manage it
      // from: it stays among the team's own secrets.
      // A paused provider is out of the listing, but its connections are kept: they show paused.
      const conns=this.connections.flatMap(c=>{
        if(c.paused) return [{id:'c'+c.id, service:c.provider, c, p:null, pasted:false,
                              name:c.provider_display_name||c.provider, st:this.connState(c)}];
        const p=this.providerIndex.get(c.provider);
        return p ? [{id:'c'+c.id, service:c.provider, c, p, pasted:this.pastedCredential(p),
                     name:p.display_name, st:this.connState(c)}] : []; });
      const named=this.namedKeys.map(({s, p, shadowed})=>({id:'s'+s.id, service:p.service, s, p, pasted:true,
        name:p.display_name, st:shadowed
          ? {key:'ok', tone:'quiet', label:'Not in use', title:'The connected '+p.display_name+' credential is used instead'}
          : {key:'ok', tone:'ok', label:'Saved', title:'Saved. It is checked on its first call.'}}));
      return [...conns, ...named].sort((a,b)=>RANK[a.st.key]-RANK[b.st.key] || byName.compare(a.name, b.name));
    },
// How many credentials the team holds per provider, named keys included: the catalog's Your account
    // / Your key mark and a provider card's Add / Replace both mean "calls here already use yours".
    connCount(){ const m={}; for(const a of this.connAccounts) if(a.service) m[a.service]=(m[a.service]||0)+1; return m; },
// The providers this server can connect: one it holds no client credentials for could only show a
    // button that does nothing. An account already connected to one still shows under Connected.
    connectable(){ return this.providers.filter(p=>p.configured); },
// The connectable providers the filter box matches, before the account / key choice.
    connMatches(){
      const q=this.connQ.trim().toLowerCase();
      return q ? this.connectable.filter(p=>[p.display_name, p.service, p.summary, p.category].join(' ').toLowerCase().includes(q))
        : this.connectable;
    },
// The account / key choice, each counting what it would show under the typed filter. "Your
    // account" is the catalog's word for the same thing ("free with your account").
    connKinds(){
      const accounts=this.connMatches.filter(p=>!this.pastedCredential(p)).length;
      return [
        {key:'', label:'All', n:this.connMatches.length, hint:'Every provider you can connect'},
        {key:'account', label:'Your account', n:accounts, hint:'Log in with an account you already have and approve access: nothing to copy'},
        {key:'key', label:'API key', n:this.connMatches.length-accounts, hint:'Paste a key from the provider'},
      ];
    },
// Every provider an account or key can be added for, by category. /oauth/providers already
    // returns them grouped then alphabetical, so this walks the list once and starts a shelf
    // whenever the category changes. Within a shelf, the account providers come first: an account
    // the team already holds is the likelier errand than a vendor key, and the sort is stable, so
    // each half keeps the registry's order.
    providerGroups(){
      const kind=this.connKind, out=[];
      for(const p of this.connMatches){
        const pasted=this.pastedCredential(p);
        if(kind && (kind==='key')!==pasted) continue;
        const cat=p.category||'Other';
        if(!out.length || out[out.length-1].category!==cat) out.push({category:cat, items:[]});
        out[out.length-1].items.push({p, pasted, n:this.connCount[p.service]||0});
      }
      for(const g of out) g.items.sort((a,b)=>a.pasted-b.pasted);
      return out;
    },
}
