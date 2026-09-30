<script>
import { useDashboard } from '../state/context'

// Bulk-loading a machine's keys and skills, for an empty Your own tools or Secrets page: the agent's
// one-line instruction, or the two CLI steps by hand. The page supplies the title and the intro.
export default { props: { title: { type: String, required: true } }, setup: useDashboard }
</script>

<template>
<section class="pl-sec">
  <div class="pl-card own-empty">
    <h2>{{title}}</h2>
    <p><slot /></p>
    <div class="seg pl-seg own-empty-seg">
      <button :class="{on:emptyTab==='agent'}" @click="emptyTab='agent'">Agent instruction</button>
      <button :class="{on:emptyTab==='manual'}" @click="emptyTab='manual'">Manual</button>
    </div>
    <template v-if="emptyTab==='manual'">
      <div class="lbl">1 · Install the CLI &amp; sign in</div>
      <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('curl -fsSL '+proxy+'/install.sh | sh\ntreg login','es1')">{{startCopied==='es1'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">curl</span> <span class="hl-flag">-fsSL</span> <span class="hl-str">{{proxy}}/install.sh</span> | sh
<span class="hl-cmd">treg</span> login</pre></div>
      <div class="lbl own-step">2 · Preview, then upload: it scans your <span class="mono">.env</span> + skill folders, you pick what to share.</div>
      <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg scan\ntreg upload --all','es3')">{{startCopied==='es3'?'✓ copied':'copy'}}</button><pre><span class="hl-comment"># preview what's here (read-only)</span>
<span class="hl-cmd">treg</span> scan
<span class="hl-comment"># register keys + skills</span>
<span class="hl-cmd">treg</span> upload <span class="hl-flag">--all</span></pre></div>
    </template>
    <template v-else>
      <p class="own-step-note">One line, token included: your agent reads llms.txt, installs the CLI, signs in, and registers your skills + keys (read-only scan first, you approve).</p>
      <div class="lc-codewrap"><button class="lc-cp" @click="copyAgentGuide('agent')">{{agentRowCopied==='agent'?'✓ copied':'copy'}}</button><pre class="own-prompt">{{buildAgentPrompt('agent', true)}}</pre></div>
    </template>
  </div>
</section>
</template>
