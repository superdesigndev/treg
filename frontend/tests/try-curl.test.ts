import { execFileSync } from 'node:child_process'
import { expect, test } from 'vitest'
import computed from '../src/state/catalogComputed.js'

// The arguments a shell hands curl when it runs the Try it line as copied.
const argv = (curl: string) => execFileSync('sh', ['-c', `${curl.replace(/^curl /, 'set -- ')}\nprintf '%s\\0' "$@"`])
  .toString().split('\0')

test('the curl line hands curl a header or form value with backslashes and quotes as typed', () => {
  const value = 'C:\\path\\" end\\'
  const curl = computed.epTryCurl.call({
    epTry: { id: 'acme.upload', method: 'POST' }, epTryQuery: '', proxy: 'https://treg.to', myToken: 'tok',
    epTryVisibleParams: [{ location: 'header', name: 'X-Note', value }],
    epTryBodyType: 'multipart', epTryMultipart: [{ name: 'title', value, file: false }], epTryBody: '',
  })
  expect(argv(curl)).toEqual(expect.arrayContaining([`X-Note: ${value}`, `title=${value}`]))
})
