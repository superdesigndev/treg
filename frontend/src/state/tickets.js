// Stale-response guard for loaders. A loader takes a ticket before its first request and checks it
// after every await: a newer call of the same loader, or (for a team-scoped loader) a switch to
// another team, makes the ticket stale, and the late answer is dropped instead of overwriting what
// the newer call shows. The counters are a handle, not state to render (see `elements`).
export function takeTicket(counters, key, team){
  const n=(counters[key]=(counters[key]||0)+1);
  const at=team ? team() : null;
  return ()=>counters[key]===n && (!team || team()===at);
}

export default {
  // ticket('orgAdmin') for a team-scoped loader; ticket('platform', false) when the answer does not
  // depend on the active team.
  ticket(key, teamScoped=true){
    const counters=this.elements.tickets||(this.elements.tickets={});
    return takeTicket(counters, key, teamScoped ? ()=>this.activeSlugNow : null); },
}
