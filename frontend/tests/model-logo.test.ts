import { expect, test } from 'vitest'
import { modelLogo } from '../src/modelLogo'

const tool = (model: object, provider = 'openrouter') => ({ provider, input: { body: { model } } })

test('a card shows the model, not the company that makes it: one maker can have several models', () => {
  const wan = modelLogo(tool({ enum: ['alibaba/wan-3.0'] }))
  const horse = modelLogo(tool({ enum: ['alibaba/happyhorse-1.1'] }))
  expect(wan).toBeTruthy()
  expect(horse).toBeTruthy()
  expect(wan).not.toBe(horse)
})

test('a new version keeps its mark without an edit', () => {
  expect(modelLogo(tool({ enum: ['google/veo-9-fast'] }))).toBe(modelLogo(tool({ enum: ['google/veo-3.1'] })))
  expect(modelLogo(tool({ enum: ['google/veo-9-fast'] }))).toBeTruthy()
})

test('Jev names its model by example, since the row accepts other versions', () => {
  expect(modelLogo(tool({ example: 'typesafe/jev-1.13' }))).toBe('/logos/jev.png')
})

test("a tool never borrows another model's mark", () => {
  expect(modelLogo(tool({ enum: ['someone/new-model'] }))).toBeUndefined()
  expect(modelLogo(tool({ enum: ['google/gemini-video'] }))).toBeUndefined()
  expect(modelLogo({ provider: 'openrouter', input: {} })).toBeUndefined()
  expect(modelLogo({ provider: 'openrouter', input: null })).toBeUndefined()
})

test("other providers' cards are untouched", () => {
  expect(modelLogo(tool({ enum: ['bytedance/seedance-2.5'] }, 'replicate'))).toBeUndefined()
})
