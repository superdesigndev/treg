// A failure in plain words: what happened and what to do, from the status and the server's detail.
export type Plain = { title: string, text: string, fixable: boolean }

function words(detail: any): string {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (detail.field && detail.rule) return `${detail.field}: ${detail.rule}`
  return detail.message || detail.hint || detail.error_message || detail.error || JSON.stringify(detail).slice(0, 400)
}

export function plain(status: number, detail: any): Plain {
  const d = detail && typeof detail === 'object' ? detail : {}
  const err = String(d.error || '')
  // The machine's network, not the tool: DNS that answered with a private address, a dropped
  // connection. "Fix it" would send the agent to change a tool that is not broken.
  if (/non-public address|ConnectError|ConnectTimeout|network|ENOTFOUND|EAI_AGAIN|getaddrinfo/i.test(words(detail)))
    return { title: 'Network problem', text: 'This machine could not reach the providers just now (its connection or DNS dropped). Nothing is wrong with the tool. Try again in a moment.', fixable: false }
  if (status === 402 || err.includes('balance') || err.includes('insufficient'))
    return { title: 'Not enough balance', text: 'Your team\'s balance is too low for this run. Top up in the dashboard, then try again.', fixable: false }
  if (err === 'manifest_invalid' || (d.field && d.rule))
    return { title: 'The files need a fix', text: words(d), fixable: true }
  if (err === 'hub_busy' || status === 429)
    return { title: 'Busy', text: 'Too many runs at once for your team. Wait a few seconds and try again.', fixable: false }
  if (status === 404 && /connect|key|not callable|no credential|access/i.test(words(d)))
    return { title: 'A step cannot be called', text: `Your team can't call one of the tools this uses yet. ${words(d)}`, fixable: true }
  if (status === 504 || err.includes('timeout'))
    return { title: 'Took too long', text: 'A step did not answer in time. Try again, or ask the agent to use a faster tool.', fixable: true }
  if (status === 0)
    return { title: 'Connection dropped', text: 'The page lost the connection. Reload to see where things got to.', fixable: false }
  return { title: status >= 500 ? 'Something failed upstream' : 'It did not work', text: words(detail) || `HTTP ${status}`, fixable: true }
}

export { words as detailWords }
