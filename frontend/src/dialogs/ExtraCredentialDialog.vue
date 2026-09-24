<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="newSkill=false">
      <div class="modal" style="width:min(680px,95vw)"><div class="hd"><b>Add a skill</b><button class="btn sm ico" @click="newSkill=false" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <template v-if="skillMode==='folder'">
            <p class="explain">Pick a skill folder (or a folder of skills). treg reads each <span class="mono">SKILL.md</span> and detects whether it's a recipe, a tool, or needs credentials - exactly like <span class="mono">treg upload skills</span> - and shows you before anything is registered.</p>
            <label class="skill-drop" v-if="!detected">
              <input type="file" webkitdirectory directory multiple @change="onSkillFolder" style="display:none"/>
              <div class="sd-ic">📁</div>
              <div><b>Choose a skill folder</b><div class="sub" style="margin:2px 0 0">reads SKILL.md, treg.json, .env + .secret files - nothing is registered until you confirm</div><div class="sub" style="margin:4px 0 0">Hidden folder like <span class="mono">.claude</span>? In the picker press <span class="mono">⌘⇧.</span> (Mac) or enable "show hidden files" to reveal it.</div></div>
            </label>
            <div v-if="skillBusy && !detected" class="sub">Analyzing…</div>
            <template v-if="detected">
              <div class="sub" style="margin:0 0 8px">{{detected.length}} skill(s) found:</div>
              <div class="skill-item" v-for="s in detected" :key="s.name" :class="{off:!skillSel[s.name]}">
                <label class="si-head">
                  <input type="checkbox" v-model="skillSel[s.name]" :disabled="s.already"/>
                  <b>{{s.name}}</b>
                  <span class="chip" :class="s.kind==='recipe_only'?'':'oauth'">{{s.kind==='recipe_only'?'recipe':'tool'}}</span>
                  <span v-if="s.already" class="chip">already registered</span>
                </label>
                <div class="si-body">
                  <div class="sub" style="margin:0">{{skillSummary(s)}}</div>
                  <div v-for="sec in s.secrets" :key="sec.name" class="si-sec">
                    <span class="mono">{{sec.name}}</span>
                    <span v-if="sec.present" class="si-ok">✓ from {{sec.source}} {{sec.ref}}</span>
                    <template v-else>
                      <span class="si-warn">needs {{sec.source}} {{sec.ref}}</span>
                      <input v-if="sec.source==='env'" v-model="skillVals[sec.ref]" type="password" :placeholder="'value for '+sec.ref" class="si-input"/>
                    </template>
                  </div>
                  <div v-for="g in s.gaps" :key="g" class="si-warn">⚠ {{g}}</div>
                  <div v-if="s.cli" class="sub" style="margin:0">{{skillCliNote(s)}}</div>
                </div>
              </div>
            </template>
            <div v-if="skillResults" style="margin-top:10px">
              <div v-for="r in skillResults" :key="r.name" class="sub" style="margin:2px 0"><span :class="r.ok?'si-ok':'si-warn'">{{r.ok?'✓':'✕'}}</span> {{r.name}}<span v-if="!r.ok"> - {{r.error}}</span></div>
            </div>
            <div v-if="skillErr" class="banner" style="margin-top:10px">{{skillErr}}</div>
            <div style="margin-top:14px;display:flex;gap:10px;align-items:center">
              <button v-if="detected" class="btn primary" @click="importSkills" :disabled="skillBusy">{{skillBusy?'Registering…':'Register selected'}}</button>
              <button v-if="detected" class="btn" @click="detected=null;skillFiles=[];skillResults=null;skillErr=''">Choose another</button>
              <a href="#" class="adv-link" @click.prevent="skillMode='json'">Advanced: paste JSON</a>
            </div>
          </template>
          <template v-else>
            <p class="explain">Raw bundle payload (recipe + secrets + tools); bindings reference a secret by its <span class="mono">local_name</span>. <a href="#" class="adv-link" @click.prevent="skillMode='folder'">back to folder import</a></p>
            <textarea v-model="skillJson" spellcheck="false" style="width:100%;height:260px;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:12px;font-family:var(--mono);font-size:12px;resize:vertical"></textarea>
            <div v-if="skillErr" class="banner" style="margin-top:10px">{{skillErr}}</div>
            <button class="btn primary" style="margin-top:10px" @click="addSkill" :disabled="skillBusy">{{skillBusy?'Registering…':'Register skill'}}</button>
          </template>
        </div></div>
    </div>
</template>
