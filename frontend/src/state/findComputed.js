import { groupBest } from './find.js'

export default {
  findActive(){ return this.find.phase!=='idle'; },
  findBusy(){ return this.find.phase==='recall' || this.find.phase==='reading'; },
  // The judge scores endpoints, but one job is usually sold by several providers (the Meta ad
  // library by three), so the answer is grouped by capability: the job is the card, the providers
  // are its lines, and the card's fit is its best provider's. Inside a card the providers keep the
  // server's order (best fit first), so the page never re-ranks them.
  findGroups(){
    return groupBest(this.find.rows, this.find.verdict, r=>(r.capability||r.id)+'|'+r.platform,
      (r, key)=>({key, label:r.capability_description||r.name, platform:r.platform, platform_label:r.platform_label}), 'rows');
  },
  findStrong(){ return this.findGroups.filter(g=>g.p!=null && g.p>=this.find.high); },
  // platform slug -> kept rows on it; drives the shelf highlight and the /search pile
  findHits(){
    const n={};
    if(this.find.phase==='done') for(const r of this.find.rows) n[r.platform]=(n[r.platform]||0)+1;
    return n;
  },
  findCandidatePlatforms(){ return [...new Set(this.find.candidates.map(c=>c.platform).filter(Boolean))]; },
}
