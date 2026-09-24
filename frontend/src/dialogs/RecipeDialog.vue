<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="viewRecipe=null">
      <div class="modal" style="width:min(760px,95vw)"><div class="hd"><b>{{viewRecipe.name}} <span class="sub" style="font-weight:400">· SKILL.md</span></b><button class="btn sm ico" @click="viewRecipe=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <textarea v-if="canRegister" v-model="viewRecipe.recipe" spellcheck="false" style="width:100%;height:52vh;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:12px;font-family:var(--mono);font-size:12.5px;line-height:1.5;resize:vertical"></textarea>
          <pre v-else class="code" style="white-space:pre-wrap;word-break:break-word;max-height:62vh">{{viewRecipe.recipe}}</pre>
          <div v-if="err" class="banner" style="margin-top:10px">{{err}}</div>
          <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
            <button v-if="canRegister" class="btn primary" @click="saveRecipe" :disabled="viewRecipe.recipe===viewRecipe.orig">{{recipeSaved?'✓ Saved':'Save changes'}}</button>
            <button class="btn" @click="copy(viewRecipe.recipe)">⧉ {{copied?'Copied!':'Copy content'}}</button>
            <span v-if="canRegister && viewRecipe.recipe!==viewRecipe.orig" class="si-warn" style="margin:0">unsaved changes</span>
          </div>
        </div></div>
    </div>
</template>
