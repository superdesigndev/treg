import { jobGroups } from './find.js'

export default {
  findActive(){ return this.find.phase!=='idle'; },
  // The full answer (Enter, or a click on the suggestion): it owns the page, shelves lit rather than
  // filtered. An auto answer (a typing pause) is a section above the still-filtered shelves.
  findFull(){ return this.findActive && !this.find.auto; },
  findBusy(){ return this.find.phase==='recall' || this.find.phase==='reading'; },
  // The judge scores endpoints, but one job is usually sold by several providers (the Meta ad
  // library by three), so the answer is grouped by capability: the job is the card, the providers
  // are its lines, and the card's fit is its best provider's. Inside a card the providers keep the
  // server's order (best fit first), so the page never re-ranks them.
  findGroups(){
    return jobGroups(this.find.rows);
  },
  findStrong(){ return this.findGroups.filter(g=>g.p!=null && g.p>=this.find.high); },
  // platform slug -> kept rows on it; drives the shelf highlight and the /search pile
  findHits(){
    const n={};
    if(this.find.phase==='done') for(const r of this.find.rows) n[r.platform]=(n[r.platform]||0)+1;
    return n;
  },
  findCandidatePlatforms(){ return [...new Set(this.find.candidates.map(c=>c.platform).filter(Boolean))]; },
  findCandidateVendors(){ return [...new Set(this.find.candidates.map(c=>c.provider).filter(Boolean))]; },
}
