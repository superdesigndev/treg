
export default {
reloadApp(){ location.reload(); },
async checkVersion(){  // index.html is served no-cache, so a plain reload picks up a new deploy —
      // this just detects one: compare /meta's bundle stamp against the one this tab booted with
      if(!this.bootVersion || this.newVersion) return;
      try{
        const m = await fetch('/meta',{headers:{'ngrok-skip-browser-warning':'1'}}).then(r=>r.json());
        if(m.app_version && m.app_version !== this.bootVersion) this.newVersion = true;
      }catch(_){}
    },
placeOrgMenu(){  // the sidebar is a scroll container that clips overflow, so the dropdown is
      // position:fixed (viewport coords) and glued to its trigger here on open/scroll/resize
      const t=this.elements.orgmain; if(!t) return;
      const r=t.getBoundingClientRect();
      this.orgMenuStyle={position:'fixed', top:(r.bottom+6)+'px', left:r.left+'px'};
    },
toggleOrgMenu(){ this.orgMenu=!this.orgMenu; if(this.orgMenu) this.placeOrgMenu(); },
closeOverlays(){ this.newTool=false; this.newSkill=false; this.showJoin=false; this.newOrg=false; this.addOrg=false; this.tryTool=null; this.copyTool=null; this.orgMenu=false; this.keyMenu=null; this.keyConfirm=null; this.share.on=false; this.methodAsk=null; if(!this.fishVoiceDialog||!this.fishVoiceDialog.busy)this.fishVoiceDialog=null; }
}
