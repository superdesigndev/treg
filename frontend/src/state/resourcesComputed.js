export default {
teamResourceProviders(){ return [...new Set(this.teamResources.map(r=>r.provider).filter(Boolean))].sort(); },
teamResourceKinds(){ return [...new Set(this.teamResources.map(r=>r.kind).filter(Boolean))].sort(); },
filteredTeamResources(){ const q=(this.q||'').trim().toLowerCase(); return this.teamResources.filter(r=>(
      (!this.teamResourceProvider||r.provider===this.teamResourceProvider) &&
      (!this.teamResourceKind||r.kind===this.teamResourceKind) &&
      (!q||[r.display_name,r.upstream_id,r.provider,r.kind,r.created_by].some(v=>String(v||'').toLowerCase().includes(q)))
    )); },
teamResourcePageCount(){ return Math.max(1,Math.ceil(this.filteredTeamResources.length/this.teamResourcePageSize)); },
teamResourceCurrentPage(){ return Math.min(this.teamResourcePage,this.teamResourcePageCount); },
pagedTeamResources(){ const start=(this.teamResourceCurrentPage-1)*this.teamResourcePageSize; return this.filteredTeamResources.slice(start,start+this.teamResourcePageSize); }
}
