import { describe, expect, it } from 'vitest'
import { build, fields, initial } from '../src/standalone/form'
import { block, blocks, safeHref, toCsv } from '../src/standalone/render'

const INPUTS = {
  domain: { type: 'string', example: 'figma.com', note: 'a bare domain' },
  limit: { type: 'int', default: 10, min: 1, max: 25 },
  ratio: { type: 'float', default: 0.5 },
  deep: { type: 'bool', default: false },
  tags: { type: 'list', example: ['a', 'b'], default: [] },
  filter: { type: 'object', default: { a: 1 } },
  key: { type: 'string', secret: true, default: '' },
}

describe('form', () => {
  it('maps every input type to a control', () => {
    const byName = Object.fromEntries(fields(INPUTS).map(f => [f.name, f]))
    expect(byName.domain).toMatchObject({ control: 'text', required: true, label: 'Domain', placeholder: 'figma.com' })
    expect(byName.limit).toMatchObject({ control: 'number', required: false, min: 1, max: 25, step: '1' })
    expect(byName.deep.control).toBe('toggle')
    expect(byName.tags).toMatchObject({ control: 'lines', placeholder: 'a\nb' })
    expect(byName.filter.control).toBe('json')
    expect(byName.key.control).toBe('secret')
  })

  it('starts from the defaults and never pre-fills a secret', () => {
    const v = initial(INPUTS)
    expect(v).toMatchObject({ domain: '', limit: '10', deep: false, tags: '', key: '' })
    expect(JSON.parse(v.filter as string)).toEqual({ a: 1 })
  })

  it('builds the body with types and leaves empty optional fields to the tool', () => {
    const v = { ...initial(INPUTS), domain: ' figma.com ', limit: '12', tags: 'x\n\n y ', ratio: '' }
    const { body, errors } = build(INPUTS, v)
    expect(errors).toEqual({})
    expect(body).toEqual({ domain: 'figma.com', limit: 12, deep: false, tags: ['x', 'y'], filter: { a: 1 } })
  })

  it('names what is wrong per field', () => {
    const { errors } = build(INPUTS, { ...initial(INPUTS), limit: '99', filter: '[1]' })
    expect(errors).toEqual({ domain: 'Required', limit: 'At most 25', filter: 'A JSON object' })
    expect(build(INPUTS, { ...initial(INPUTS), domain: 'x', limit: '2.5' }).errors.limit).toBe('A whole number')
  })
})

describe('render', () => {
  it('chooses a block by the shape of the value', () => {
    expect(block('n', 1234).kind).toBe('value')
    expect(block('e', []).kind).toBe('empty')
    expect(block('u', 'https://x.com/a')).toMatchObject({ kind: 'link', href: 'https://x.com/a' })
    expect(block('i', 'https://x.com/a.png?w=1')).toMatchObject({ kind: 'image' })
    expect(block('l', ['a', 2])).toMatchObject({ kind: 'list', items: ['a', '2'] })
    const t = block('rows', [{ a: 1, b: 'x' }, { a: 2, c: true }])
    expect(t).toMatchObject({ kind: 'table', columns: ['a', 'b', 'c'], rows: [['1', 'x', ''], ['2', '', 'yes']] })
    expect(block('o', { a: { b: { c: { d: 1 } } } }).kind).toBe('section')
  })

  it('never makes a link of a non-http value', () => {
    expect(block('x', 'javascript:alert(1)').kind).toBe('value')
    expect(safeHref('javascript:alert(1)')).toBeNull()
    expect(safeHref('data:text/html,<b>')).toBeNull()
  })

  it('orders declared fields first and sets short values side by side', () => {
    const out = blocks({ z: 1, b: 2, a: 3, rows: [{ x: 1 }] }, ['a', 'b'])
    expect(out[0]).toMatchObject({ kind: 'tiles', items: [{ label: 'a' }, { label: 'b' }, { label: 'z' }] })
    expect(out[1].kind).toBe('table')
    expect(blocks({ only: 1 }, ['only'])[0].kind).toBe('value')
  })

  it('quotes CSV cells that need it', () => {
    expect(toCsv(['a', 'b'], [['1,2', 'say "hi"'], ['x', 'y\nz']])).toBe('a,b\n"1,2","say ""hi"""\nx,"y\nz"\n')
  })
})
