// One unacknowledged issuance per org, owned by this dashboard instance only. A navigation
// ticket may discard a read; it must never discard a successful, already committed key rotation.
export const credentialIssueComputed = {
  credentialIssue(){
    if(this.activeOrg?.slug!==this.activeSlugNow) return null;
    return this.credentialIssues[this.activeOrgId]||null;
  },
  newAgent(){ const issue=this.credentialIssue;
    return this.canAdmin && issue?.type==='agent' && issue.status==='ready' ? issue.result : null; },
  newApiKey(){ const issue=this.credentialIssue;
    return issue?.type==='key' && issue.status==='ready'
      && (issue.result.assigned_type!=='agent' || this.canAdmin) ? issue.result : null; },
};

export default {
  beginCredentialIssue(type){
    // Serialize through acknowledgement, including across org/page switches and both rotate
    // controls. A second POST must not revoke a secret the user has not had a chance to save.
    if(this.credentialIssue){ this.orgTab=this.credentialIssue.type==='agent'?'members':'keys'; return null; }
    if(!this.activeOrgId || this.activeOrg?.slug!==this.activeSlugNow) return null;
    const id=this.activeOrgId;
    this.credentialIssues[id]={org_id:id, org:this.activeSlugNow, type, status:'pending', result:null, error:''};
    return this.credentialIssues[id];
  },
  async finishCredentialIssue(issue, result){
    if(this.credentialIssues[issue.org_id]!==issue) return; // logout / app teardown
    issue.result={...result, org_id:issue.org_id, org:issue.org};
    issue.status='held';
    // Off-page/off-org results stay in memory. Only reads are cancelled or retried.
    if(this.credentialIssue===issue) await this.resumeCredentialIssue();
  },
  failCredentialIssue(issue){
    if(this.credentialIssues[issue.org_id]!==issue || issue.status!=='pending') return;
    issue.status='failed';
    issue.error='The key request could not be confirmed. It may have completed. Check API Keys before issuing another key.';
  },
  parkCredentialIssue(orgId=this.activeOrgId){
    const read=this.elements.credentialRead;
    if(read?.issue.org_id===orgId){ this.elements.credentialRead=null; read.controller.abort(); }
    const issue=this.credentialIssues[orgId];
    if(issue && ['ready','checking'].includes(issue.status)) issue.status='held';
    this.stopAgentPoll();
  },
  invalidateCredentialIssue(target){
    const issue=this.credentialIssues[target.org_id];
    if(issue?.result!==target) return;
    issue.result=null; issue.status='unavailable';
    issue.error='This key is no longer active or accessible. Check API Keys before issuing a replacement.';
    this.stopAgentPoll();
  },
  async resumeCredentialIssue(){
    const issue=this.credentialIssue;
    if(this.view!=='orgs' || !issue?.result || issue.status!=='held') return;
    const result=issue.result;
    const agent=issue.type==='agent' || result.assigned_type==='agent';
    if(agent && !this.canAdmin) return;
    const read={issue, controller:new AbortController()};
    this.elements.credentialRead=read; issue.status='checking'; issue.error='';
    const live=()=>this.elements.credentialRead===read && !read.controller.signal.aborted
      && this.view==='orgs' && this.credentialIssue===issue && issue.result===result;
    const options={signal:read.controller.signal, cache:'no-store'};
    try{
      // Check before revealing on receipt/return: another session may have replaced the key.
      // Agent status is the lightweight endpoint, not a roster or history refresh.
      if(agent){
        const status=await this.api('/orgs/'+issue.org_id+'/agents/'+result.user_id+'/connection?api_key_id='+result.api_key_id, options);
        if(!live()) return;
        result.connected=status.connected;
      }else if(result.kind==='default_human'){
        const current=await this.api('/auth/cli-token', options);
        if(!live()) return;
        if(current.default_key_state!=='active' || current.token!==result.secret){ this.invalidateCredentialIssue(result); return; }
        this.myToken=result.secret; this.defaultKeyId=current.default_key_id; this.defaultKeyState='active';
        this._myTokenOrg=this.activeSlugNow; this.startTokenShow=false;
      }else{
        const keys=await this.api('/orgs/'+issue.org_id+'/api-keys', options);
        if(!live()) return;
        if(!keys.some(key=>key.id===result.id && key.state==='active')){ this.invalidateCredentialIssue(result); return; }
      }
      result.org=this.activeSlugNow; // a team rename must not leave stale setup instructions
      issue.status='ready';
    }catch(e){
      if(!live()) return;
      if([401,403,404].includes(e.status)) this.invalidateCredentialIssue(result);
      else{ issue.status='held'; issue.error='Your key is saved in this tab, but its status could not be checked. Check again before copying it.'; }
    }finally{ if(this.elements.credentialRead===read) this.elements.credentialRead=null; }
  },
  dismissCredentialIssue(){
    const issue=this.credentialIssue;
    if(!issue || ['pending','checking'].includes(issue.status)) return;
    this.parkCredentialIssue();
    issue.result=null;
    delete this.credentialIssues[issue.org_id];
    this.snipAgent=null;
  },
  clearCredentialIssues(){
    for(const id of Object.keys(this.credentialIssues)){
      this.parkCredentialIssue(Number(id));
      this.credentialIssues[id].result=null;
    }
    this.credentialIssues={};
  },
};
