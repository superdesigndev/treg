import { workbenchHash, EMPTY_ROUTE } from '../workbench/routes'

export default {
workbenchUrl(){ return '/app'+workbenchHash(this.wb); },
// Move inside the Workbench: a tab, a table, a run, a cell. The address follows (syncTabUrl).
goWorkbench(route, fromPop){
  this.wb={...EMPTY_ROUTE, ...this.wb, ...route};
  if(this.view!=='workbench') this.go('workbench', fromPop);
  else if(!fromPop) history.pushState({view:'workbench'}, '', this.workbenchUrl()); },
}
