<script setup lang="ts">
// A provider's logo tile (`.pl-logo`). A logo that fails to load leaves the tile blank rather than
// a broken image; the name beside it says who it is.
// `src` puts another mark, such as a model's, in the same tile.
defineProps<{ service: string, large?: boolean, src?: string }>()
const hide = (e: Event) => { (e.target as HTMLImageElement).style.visibility = 'hidden' }
</script>

<template>
<span class="pl-logo" :class="{ lg: large }"><img :src="src || '/logos/' + service + '.svg'" alt="" aria-hidden="true" @error="hide"></span>
</template>
