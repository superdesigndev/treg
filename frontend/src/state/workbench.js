import { workbenchHash, toolsHash, EMPTY_ROUTE } from '../workbench/routes'

export default {
workbenchUrl(){ return '/app'+workbenchHash(this.wb); },
// Move inside the Workbench: a tab, a table, a run, a cell. The address follows (syncTabUrl).
goWorkbench(route, fromPop){
  this.wb={...EMPTY_ROUTE, ...this.wb, ...route};
  if(this.view!=='workbench') this.go('workbench', fromPop);
  else if(!fromPop) history.pushState({view:'workbench'}, '', this.workbenchUrl()); },
toolsUrl(){ return '/app'+toolsHash(this.wbTools); },
// A tool definition or an install page: #tools/t/<id> and #tools/i/<name>, each with a tab.
goTools(route, fromPop){
  this.wbTools={...this.wbTools, ...route};
  const view=this.wbTools.kind==='tool' ? 'wbtool' : 'wbinstall';
  if(this.view!==view) this.go(view, fromPop);
  else if(!fromPop) history.pushState({view}, '', this.toolsUrl()); },
}
