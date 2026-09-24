<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="callView=null" style="place-items:stretch;justify-items:end">
      <div class="drawer" style="width:min(680px,96vw)"><div class="hd" style="padding:15px 18px;border-bottom:1px solid var(--line)"><b>{{callView.endpoint_id||callView.tool||'Call'}}</b><button class="btn sm" @click="callView=null" aria-label="Close">✕</button></div>
        <div class="bd" style="padding:16px 18px;overflow:auto">
          <div class="kv">
            <div><b>When</b>{{when(callView.created_at)}}</div>
            <div><b>Status</b><span class="badge" :class="callView.status_code<400?'ok':'invalid'">{{callView.status_code}}</span></div>
            <div v-if="callView.provider"><b>Provider</b>{{callView.provider}}</div>
            <div v-if="callView.credential_tier"><b>Served on</b>{{servedOn(callView.credential_tier)}}</div>
            <div v-if="callView.cached"><b>Answer</b><span class="chip" title="served from the archive instead of the vendor">cached</span></div>
            <div v-if="callView.call_ref"><b>Call id</b><span class="mono" style="font-size:11px">{{callView.call_ref}}</span></div>
          </div>
          <!-- A generation task's result, shown rather than linked: the video or image the caller paid
               for is the point of the call, and the archived body is only the submission receipt. -->
          <template v-if="callView.task">
            <h3 style="margin:10px 0 6px;font-size:13px;display:flex;align-items:center;gap:8px">Result
              <span class="chip" :title="taskStateTitle(callView.task)">{{taskStateLabel(callView.task)}}</span>
              <a v-if="callView.task.result_url" :href="callView.task.result_url" target="_blank" rel="noopener" style="margin-left:auto;font-size:12px">open ↗</a>
            </h3>
            <template v-if="callView.task.result_url">
              <video v-if="isVideoUrl(callView.task.result_url)" :src="callView.task.result_url" controls playsinline style="width:100%;max-height:60vh;background:#000;border-radius:8px"></video>
              <img v-else :src="callView.task.result_url" alt="generated result" style="max-width:100%;max-height:60vh;border-radius:8px;display:block"/>
              <p class="sub" style="margin:6px 0 12px">{{callView.task.ttl_note?'The provider keeps this file for '+callView.task.ttl_note+' — download it to keep it.':'Time-limited link — download it to keep it.'}}</p>
            </template>
            <p v-else-if="callView.task.fetch_command" class="sub" style="margin:0 0 12px">Retrieve it from the CLI: <span class="mono">{{callView.task.fetch_command}}</span></p>
            <p v-else-if="callView.task.error" class="sub" style="margin:0 0 12px;color:var(--red)">{{callView.task.error}}</p>
            <p v-else-if="callView.task.status==='pending'" class="sub" style="margin:0 0 12px">Still generating — the result appears here when the provider finishes.</p>
          </template>
          <p v-if="callView.loading" class="sub">Loading…</p>
          <p v-else-if="callView.error" class="sub" style="color:var(--red)">{{callView.error}}</p>
          <template v-else>
            <p v-if="callView.note" class="sub" style="margin:4px 0 12px">{{callView.note}}</p>
            <template v-if="callView.request">
              <h3 style="margin:10px 0 6px;font-size:13px">Request</h3>
              <p class="explain" style="margin:0 0 6px"><span class="mono">{{callView.request.method}} {{callView.request.url}}</span></p>
              <pre v-if="callView.request.body_text" style="max-height:24vh">{{pretty(callView.request.body_text)}}</pre>
            </template>
            <template v-if="callView.response">
              <h3 style="margin:14px 0 6px;font-size:13px;display:flex;align-items:center;gap:8px">Response
                <span class="muted" style="font-weight:400;font-size:11px">{{callView.response.status_code}} · {{callView.response.media_type||'—'}} · {{fmtBytes(callView.response.size_bytes)}} · fetched {{when(callView.response.fetched_at)}}</span>
                <button v-if="callView.response.body_text" class="btn sm" style="margin-left:auto" @click="copyCallBody()">{{callCopied||'Copy'}}</button>
              </h3>
              <template v-if="callView.response.body_text">
                <pre style="max-height:60vh">{{callBodyShown}}</pre>
                <p v-if="callBodyTruncated" class="sub" style="margin:6px 0 0"><a href="#" @click.prevent="callViewFull=true">Show full response ({{fmtBytes(callView.response.body_text.length)}})</a></p>
              </template>
              <p v-else-if="callView.stored" class="sub">The stored answer is not text — copy is unavailable for binary bodies.</p>
            </template>
          </template>
        </div>
      </div>
    </div>
</template>
