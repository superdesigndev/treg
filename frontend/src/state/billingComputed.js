export default {
topupUsd(){ if(!this.billing) return 0; if(this.topupPick==='other'){ const v=Number(this.topupOther); return Number.isInteger(v)&&v>0?v:0; } return this.topupPick; },
topupValid(){ if(!this.billing||!this.topupUsd) return false; const t=this.billing.topup; return this.topupUsd>=t.min_usd&&this.topupUsd<=t.max_usd; },
topupBonusMicro(){ return this.tierBonus(this.topupUsd)+this.refPresetBonus(this.topupUsd); },
maxBonusPct(){ const t=this.billing&&this.billing.topup.bonus_tiers; return t?Math.max(0,...Object.values(t)):0; }
}
