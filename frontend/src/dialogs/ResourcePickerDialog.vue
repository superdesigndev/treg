<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div  class="scrim" role="dialog" aria-modal="true" @click.self="resPick=null">
      <div class="modal" style="padding:16px">
        <h3 style="margin:0 0 10px">Choose {{article(resPick.label)}} {{resPick.label}}</h3>
        <p class="sub" style="margin:0 0 10px">The {{resPick.label}} your agent uses by default.
          It can still use another one per call — this just saves it guessing.</p>
        <div v-if="resPick.loading" class="ttable-wrap">
          <div style="padding:22px;text-align:center;color:var(--muted);font-family:var(--mono);font-size:12.5px">
            <span class="spin" aria-hidden="true"></span> Asking the provider which {{resPick.plural}} you can use…
          </div>
        </div>
        <div v-else-if="resPick.err" class="banner" style="margin:0 0 10px">{{resPick.err}}</div>
        <p class="sub" v-else-if="!resPick.rows.length" style="margin:0 0 10px">No {{resPick.plural}} found — this account may not have access to any.</p>
        <div class="ttable-wrap" v-else><table class="ttable">
          <tr v-for="r in resPick.rows" :key="r.id">
            <td class="tn"><b>{{r.label||r.id}}</b><span class="sub" style="display:block;font-size:11px">{{r.id}}</span></td>
            <td class="tx"><button class="btn sm" :class="{primary:r.id===resPick.selected}" @click="chooseResource(r)">{{r.id===resPick.selected?'✓ Current':'Use this'}}</button></td>
          </tr>
        </table></div>
        <div style="margin-top:12px;text-align:right"><button class="btn sm" @click="resPick=null">Close</button></div>
      </div>
    </div>
</template>
