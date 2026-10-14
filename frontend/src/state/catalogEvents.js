// ---- catalog events ----
// One event set for every catalog surface, so a shelf, a comparison, a provider page and the Catalog
// index read on the same numbers: `catalog_platform_viewed`, `catalog_comparison_viewed`,
// `catalog_tool_opened`, and `catalog_action` for the actions that say "I found what I came for".
export default {
  // Where a catalog action happened: a shelf, a comparison, a provider's page, or the Catalog index.
  // null elsewhere, so a Try-it from the team pages is not counted.
  catalogSurface(){
    if(this.view==='platform') return this.platCap ? 'comparison' : 'shelf'
    if(this.view==='provider') return 'provider'
    if(this.view==='catalog') return 'catalog'
    return null
  },
  catalogTrack(name, props){
    const surface=this.catalogSurface(); if(!surface) return
    this.track(name, {surface, platform:this.view==='platform' ? this.platSlug : undefined,
      signed_in:!!this.authed, ...props})
  },
  catalogToolEvent(name, e, props){
    this.catalogTrack(name, {endpoint:e.id, provider:e.provider, capability:e.capability||undefined, ...props})
  },

  // A signed-out visitor's Try it and Connect lead to sign-in; they count, with `signed_in:false`.
  catalogTry(e){
    this.catalogToolEvent('catalog_action', e, {action:'try'})
    if(this.publicCatalog) this.openSignin(); else this.openEpTry(e)
  },
  catalogConnect(e){
    this.catalogToolEvent('catalog_action', e, {action:'connect'})
    if(this.publicCatalog) this.openSignin(); else this.openProvider(e.provider)
  },
  catalogByok(service, e){
    this.catalogTrack('catalog_action', {action:'byok', provider:service||undefined, endpoint:e ? e.id : undefined})
    if(this.publicCatalog) this.openSignin(); else this.goByok(service)
  },
  catalogCopy(e){
    this.catalogToolEvent('catalog_action', e, {action:'copy'})
    this.copyCall(e)
  },
  catalogDocs(e){ this.catalogToolEvent('catalog_action', e, {action:'docs'}) },
}
