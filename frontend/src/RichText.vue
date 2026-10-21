<script setup lang="ts">
import {computed} from 'vue'
const props=defineProps<{text:string}>()
const parts=computed(()=>String(props.text||'').split(/(\*\*[^*\n]+\*\*)/g))
</script>
<template><p class="answer"><template v-for="(part,i) in parts" :key="i"><strong v-if="part.startsWith('**')&&part.endsWith('**')">{{part.slice(2,-2)}}</strong><template v-else>{{part}}</template></template></p></template>
