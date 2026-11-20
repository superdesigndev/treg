// Picture uploads (POST /images/{kind}): the raw file is the body, its type the Content-Type.
// The server checks and re-draws every picture; the checks here only stop an upload that cannot
// pass, with the same words, before the bytes leave the browser.

export type ImageKind = 'user_avatar' | 'team_avatar' | 'showcase_icon' | 'showcase_shot'
export type Staged = { key: string, kind: string, preview_url: string }

const MB = 1_000_000
export const IMAGE_RULES: Record<ImageKind, { maxBytes: number, hint: string }> = {
  user_avatar: { maxBytes: 2 * MB, hint: 'PNG, JPEG or WebP, at least 64×64, up to 2 MB.' },
  team_avatar: { maxBytes: 2 * MB, hint: 'PNG, JPEG or WebP, at least 64×64, up to 2 MB.' },
  showcase_icon: { maxBytes: 2 * MB, hint: 'Square PNG, JPEG or WebP, at least 96×96 (512×512 looks sharpest), up to 2 MB.' },
  showcase_shot: { maxBytes: 5 * MB, hint: 'PNG, JPEG or WebP, desktop or phone, at least 600 px on the longest side, up to 5 MB.' },
}
const TYPES = ['image/png', 'image/jpeg', 'image/webp']

export class UploadError extends Error {
  constructor(readonly status: number, readonly detail: string) { super(detail) }
}

/** Why this file cannot be uploaded as `kind`, or '' when it may be. */
export function imageProblem(kind: ImageKind, file: { type: string, size: number }): string {
  if (!TYPES.includes(file.type)) return 'Use a PNG, JPEG or WebP image.'
  if (!file.size) return 'The file is empty.'
  if (file.size > IMAGE_RULES[kind].maxBytes) return `The file is too large (up to ${IMAGE_RULES[kind].maxBytes / MB} MB).`
  return ''
}

/** Stage one picture. Throws UploadError with the server's words; 503 means uploads are off. */
export async function uploadImage(kind: ImageKind, file: Blob, headers: Record<string, string>,
                                  fetchImpl: typeof fetch = fetch): Promise<Staged> {
  const r = await fetchImpl('/images/' + kind, {
    method: 'POST', credentials: 'include', body: file,
    headers: { ...headers, 'content-type': file.type },
  })
  const body = await r.json().catch(() => null)
  if (!r.ok) {
    const detail = body && typeof body.detail === 'string' ? body.detail : 'the image could not be uploaded (' + r.status + ')'
    throw new UploadError(r.status, detail)
  }
  return body as Staged
}

/** Two letters for a picture-less person or team. */
export function initialsOf(text: string | null | undefined): string {
  const t = (text || '').trim()
  if (!t) return '?'
  const words = t.split('@')[0]!.split(/[\s._-]+/).filter(Boolean)
  if (words.length > 1) return (words[0]![0]! + words[1]![0]!).toUpperCase()
  return t.slice(0, 2).toUpperCase()
}
