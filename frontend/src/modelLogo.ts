// The mark of the model behind an OpenRouter tool. Unmodified brand marks: most come from the pinned
// LobeHub package; Wan and HeyGen, which it lacks, are those brands' own site icons.
import deepmind from '@lobehub/icons-static-svg/icons/deepmind-color.svg'
import flux from '@lobehub/icons-static-svg/icons/flux.svg'
import grok from '@lobehub/icons-static-svg/icons/grok.svg'
import hailuo from '@lobehub/icons-static-svg/icons/hailuo-color.svg'
import happyhorse from '@lobehub/icons-static-svg/icons/happyhorse.svg'
import kling from '@lobehub/icons-static-svg/icons/kling-color.svg'
import runway from '@lobehub/icons-static-svg/icons/runway.svg'
import sora from '@lobehub/icons-static-svg/icons/sora-color.svg'
import heygen from './assets/models/heygen.png'
import wan from './assets/models/wan.png'

// Matched as a prefix of the model id, so a new version of a listed model needs no edit.
const MARKS: [prefix: string, src: string][] = [
  ['alibaba/happyhorse', happyhorse],
  ['alibaba/wan', wan],
  ['black-forest-labs/flux', flux],
  ['bytedance/seedance', '/logos/platforms/seedance.svg'],
  ['google/veo', deepmind],
  ['heygen/', heygen],
  ['kwaivgi/kling', kling],
  ['minimax/hailuo', hailuo],
  ['openai/sora', sora],
  ['runway/', runway],
  ['typesafe/jev', '/logos/jev.png'],
  ['x-ai/grok', grok],
]

type Tool = { provider?: string, input?: { body?: { model?: { enum?: string[], example?: string } } } | null }

/** Undefined when the tool names no model or one without a mark: the card then shows no logo. */
export function modelLogo(tool: Tool): string | undefined {
  if (tool.provider !== 'openrouter') return undefined
  const model = tool.input?.body?.model
  const id = model?.enum?.[0] ?? model?.example
  return MARKS.find(([prefix]) => id?.startsWith(prefix))?.[1]
}
