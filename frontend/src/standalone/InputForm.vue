<script setup lang="ts">
import { computed } from 'vue'
import { fields, type InputSpec } from './form'

const props = defineProps<{ inputs: Record<string, InputSpec>, errors: Record<string, string>, disabled?: boolean }>()
const values = defineModel<Record<string, unknown>>({ required: true })
const list = computed(() => fields(props.inputs))
</script>

<template>
  <div class="sa-fields">
    <p v-if="!list.length" class="sa-muted">This tool takes no inputs.</p>
    <div v-for="f in list" :key="f.name" class="sa-field" :class="{ bad: errors[f.name] }">
      <label v-if="f.control !== 'toggle'" :for="`in-${f.name}`">
        {{ f.label }}<span v-if="f.required" class="sa-req" aria-label="required"> *</span>
      </label>
      <input v-if="f.control === 'text' || f.control === 'secret'" :id="`in-${f.name}`" v-model="values[f.name]"
             :type="f.control === 'secret' ? 'password' : 'text'" :placeholder="f.placeholder" :disabled="disabled"
             :autocomplete="f.control === 'secret' ? 'off' : 'on'" :aria-invalid="!!errors[f.name]"/>
      <input v-else-if="f.control === 'number'" :id="`in-${f.name}`" v-model="values[f.name]" type="number"
             :min="f.min" :max="f.max" :step="f.step" :placeholder="f.placeholder" :disabled="disabled"
             :aria-invalid="!!errors[f.name]"/>
      <label v-else-if="f.control === 'toggle'" class="sa-toggle">
        <input :id="`in-${f.name}`" v-model="values[f.name]" type="checkbox" :disabled="disabled"/> {{ f.label }}
      </label>
      <textarea v-else :id="`in-${f.name}`" v-model="values[f.name] as string" :rows="f.control === 'textarea' ? 3 : 5"
                :placeholder="f.placeholder" :disabled="disabled" :class="{ mono: f.control === 'json' }"
                :aria-invalid="!!errors[f.name]"/>
      <p v-if="errors[f.name]" class="sa-error-text">{{ errors[f.name] }}</p>
      <p v-else-if="f.help || f.control === 'lines'" class="sa-help">
        {{ f.help }}<span v-if="f.control === 'lines'">{{ f.help ? ' · ' : '' }}One per line.</span>
        <span v-if="f.min !== undefined || f.max !== undefined">{{ f.help ? ' · ' : '' }}{{ f.min ?? '' }}{{ f.min !== undefined && f.max !== undefined ? ' to ' : f.max !== undefined ? 'up to ' : ' or more' }}{{ f.max ?? '' }}</span>
      </p>
    </div>
  </div>
</template>
