<script setup lang="ts">
import { computed } from 'vue';
import { LayoutGrid, Utensils, Mic, Scissors, Dumbbell, Flower2, Sparkles, Baby, Martini, PartyPopper, Brush } from '@lucide/vue';

const props = defineProps<{ id?: number | string | null }>();

const icons: Record<string, unknown> = {
    '1': Utensils,   // 美食
    '2': Mic,        // KTV
    '3': Scissors,   // 丽人·美发
    '4': Dumbbell,   // 健身运动
    '5': Flower2,    // 按摩·足疗
    '6': Sparkles,   // 美容SPA
    '7': Baby,       // 亲子游乐
    '8': Martini,    // 酒吧
    '9': PartyPopper,// 轰趴馆
    '10': Brush,     // 美睫·美甲
};

const icon = computed(() => {
    const key = props.id === undefined || props.id === null ? '' : String(props.id);
    return icons[key] || LayoutGrid;
});
</script>

<template>
  <component :is="icon" :size="18" :stroke-width="1.6" class="type-icon" aria-hidden="true" />
</template>
