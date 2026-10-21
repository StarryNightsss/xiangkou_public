<script setup lang="ts">
defineOptions({ name: 'Chat' });
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAccount } from './store'
import { api, statusName } from './api';
import { readEvents } from './stream.mjs';
import ShopCard from './ShopCard.vue';
import RichText from './RichText.vue';
import ActionDialog from './ActionDialog.vue';
import type { TaskCheckpoint, PersonalOrder } from './types';

// HTTP（非安全上下文）下 crypto.randomUUID 不存在，直接调用会抛 TypeError。
function uuid(): string {
    const cryptoApi = globalThis.crypto as Crypto | undefined;
    if (cryptoApi && typeof cryptoApi.randomUUID === 'function')
        return cryptoApi.randomUUID();
    const bytes = new Uint8Array(16);
    if (cryptoApi && typeof cryptoApi.getRandomValues === 'function')
        cryptoApi.getRandomValues(bytes);
    else
        for (let i = 0; i < bytes.length; i += 1)
            bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0'));
    return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`;
}

const route = useRoute(), router = useRouter(), account = useAccount();
const messages = ref<any[]>([]), input = ref(''), busy = ref(false), error = ref(''), session = ref<string | null>(null), context = ref<string | null>(null), draft = ref<any>(null), trace = ref<any>(null), list = ref<HTMLElement>(), tasks = ref<TaskCheckpoint[]>([]);
let controller: AbortController | null = null, requestId: string | null = null, revision = 0;
async function stop() {
    for (const m of messages.value)
        if (m.pending) {
            m.pending = false;
            m.text = '已停止生成，可继续提问。';
        }
    messages.value = [...messages.value];
    revision++;
    controller?.abort();
    controller = null;
    const id = requestId;
    requestId = null;
    busy.value = false;
    if (id) {
        try {
            await api(`/api/requests/${id}`, 'DELETE');
        }
        catch {
        }
    }
}
async function scroll() {
    await nextTick();
    if (list.value)
        list.value.scrollTop = list.value.scrollHeight;
}
async function load() {
    const target = String(route.params.id || '') || null;
    const stopping = stop();
    const rev = revision;
    await stopping;
    if (rev !== revision)
        return;
    error.value = '';
    messages.value = [];
    draft.value = null;
    context.value = null;
    session.value = target;
    if (target) {
        busy.value = true;
        try {
            const data = await api(`/api/sessions/${target}/messages`);
            if (rev !== revision)
                return;
            messages.value = data.messages;
            context.value = data.last_task_response?.recommendation_run_id || data.last_task_response?.run_id || data.last_response?.recommendation_run_id || data.last_response?.run_id || null;
            await scroll();
        }
        catch (e) {
            if (rev === revision)
                error.value = (e as Error).message;
        }
        finally {
            if (rev === revision)
                busy.value = false;
        }
    }
}
watch(() => route.params.id, () => {
    if ((route.params.id || null) !== session.value)
        load();
}, { immediate: true });
async function refreshSessions(rev: number) {
    try {
        await account.refreshSessions();
    }
    catch {
        if (rev === revision)
            error.value = '回答已保留，最近会话列表刷新失败，可稍后重试。';
    }
}
async function send(text?: string) {
    const message = text || input.value.trim();
    if (!message || busy.value)
        return;
    error.value = '';
    input.value = '';
    busy.value = true;
    const rev = ++revision;
    requestId = uuid();
    const abort = new AbortController();
    controller = abort;
    messages.value.push({ id: uuid(), role: 'user', text: message });
    const reply: any = { id: uuid(), role: 'assistant', text: '正在理解你的需求…', pending: true };
    messages.value.push(reply);
    let result: any;
    try {
        await scroll();
        if (rev !== revision)
            return;
        const response = await fetch('/api/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, session_id: session.value, context_run_id: context.value, request_id: requestId }), signal: abort.signal });
        if (!response.ok) {
            if (response.status === 401)
                account.clear();
            throw new Error((await response.json()).detail || '请求失败');
        }
        result = await readEvents(response, item => {
            if (rev !== revision)
                return;
            if (item.type === 'event' && item.data.event === 'session_started')
                session.value = item.data.session_id;
            if (item.type === 'event' && item.data.event === 'tool_start') {
                reply.text = '正在执行业务查询与校验…';
                messages.value = [...messages.value];
            }
        });
        if (rev !== revision)
            return;
        reply.text = result.answer;
        reply.result = result;
        reply.run_id = result.run_id;
        reply.pending = false;
        messages.value = [...messages.value];
        session.value = result.session_id || session.value;
        context.value = result.recommendation_run_id || (result.shops?.length ? result.run_id : context.value);
        if (result.action_draft)
            draft.value = result.action_draft;
        if (session.value)
            await router.replace('/chat/' + session.value);
        await refreshSessions(rev);
        await scroll();
    }
    catch (e) {
        if (rev === revision) {
            reply.pending = false;
            reply.text = (e as Error).name === 'AbortError' ? '已停止生成' : (e as Error).message;
            messages.value = [...messages.value];
        }
    }
    finally {
        // 被 load()/stop() 打断时 rev 会失配、controller 已被清空；
        // 这里仍要复位 busy，否则界面永远停在「停止」，后续消息被 busy 静默忽略。
        if (rev === revision || controller === null) {
            busy.value = false;
            requestId = null;
            controller = null;
        }
    }
}
async function buy(shop: any, coupon: any, result: any) {
    if (busy.value)
        return;
    try {
        draft.value = await api('/api/actions/vouchers/prepare', 'POST', { run_id: result.recommendation_run_id || result.run_id, shop_id: shop.id, voucher_id: coupon.id });
    }
    catch (e) {
        error.value = (e as Error).message;
    }
}
async function showTrace(id: string) {
    try {
        trace.value = await api(`/api/runs/${id}`);
    }
    catch (e) {
        error.value = (e as Error).message;
    }
}
async function showTasks() {
    try {
        tasks.value = (await api<{
            tasks: TaskCheckpoint[];
        }>('/api/tasks')).tasks;
        if (!tasks.value.length)
            error.value = '暂无任务记录';
    }
    catch (e) {
        error.value = (e as Error).message;
    }
}
async function resume(id: string) {
    if (busy.value)
        return;
    const stopping = stop();
    const rev = revision;
    busy.value = true;
    error.value = '';
    try {
        await stopping;
        if (rev !== revision)
            return;
        const result = await api(`/api/tasks/${id}/resume`, 'POST');
        if (rev !== revision)
            return;
        if (!result.session_id)
            throw new Error('任务没有关联会话');
        const history = await api(`/api/sessions/${result.session_id}/messages`);
        if (rev !== revision)
            return;
        messages.value = history.messages;
        session.value = result.session_id;
        context.value = result.recommendation_run_id || result.run_id || null;
        draft.value = result.action_draft || null;
        if (!messages.value.some(m => m.run_id === result.run_id))
            messages.value.push({ id: uuid(), role: 'assistant', text: result.answer, result, run_id: result.run_id });
        await router.replace('/chat/' + result.session_id);
        await refreshSessions(rev);
        await scroll();
    }
    catch (e) {
        if (rev === revision)
            error.value = (e as Error).message;
    }
    finally {
        if (rev === revision)
            busy.value = false;
    }
}
function onComposerKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.isComposing || event.keyCode === 229)
        return;
    event.preventDefault();
    void send();
}
function orderTitle(order: PersonalOrder) {
    return order.title || order.voucher_title || order.voucherTitle || '商品券';
}
function taskPhase(phase: string) {
    return ({ planned: '已规划', retryable: '可重试', interrupted: '已中断', completed: '已完成', resumed: '已恢复' } as Record<string, string>)[phase] || '执行中';
}
function newChat() {
    load();
}
onMounted(() => {
    window.addEventListener('new-chat', newChat);
    const message = sessionStorage.getItem('xiangkou-handoff');
    if (message) {
        sessionStorage.removeItem('xiangkou-handoff');
        input.value = message;
    }
});
onUnmounted(() => {
    stop();
    window.removeEventListener('new-chat', newChat);
});
</script>
<template><div class="chat-layout"><div class="chat-toolbar"><span class="muted small">{{session?'正在继续同一会话':'开启一个新计划'}}</span><button class="outline" :disabled="busy" @click="showTasks">任务记录</button><button class="outline" :disabled="!messages.some(m=>m.run_id)" @click="showTrace([...messages].reverse().find(m=>m.run_id).run_id)">运行记录 ↗</button></div><div class="error" v-if="error" role="alert">{{error}}</div><div class="transcript" ref="list"><div class="welcome" v-if="!messages.length"><div class="eyebrow">YOUR CITY, YOUR WAY</div><h2>下一站，想去哪里？</h2><p>找一家好店，看看大家的体验，安排你的城市生活。</p><div class="suggestions"><button @click="input='帮我找火锅店，人均150以内'">找一家火锅店</button><button @click="input='我想订个KTV'">安排朋友聚会</button><button @click="input='查一下我的订单'">查看我的订单</button></div></div><article v-for="m in messages" :key="m.id" :class="['message',m.role]"><div class="speaker">{{m.role==='user'?'你':'巷口'}}</div><div class="message-body"><RichText :text="m.text"/><template v-if="m.result"><div class="conditions"><span v-for="c in m.result.confirmed_constraints" :key="c" class="pill">{{c}}</span></div><div class="shop-grid"><ShopCard v-for="s in m.result.shops" :key="s.id" :shop="s" @detail="router.push('/shops/'+s.id)" @buy="buy(s,$event,m.result)"/></div><div class="itinerary" v-for="(it,i) in m.result.itineraries" :key="i"><h3>方案 {{i+1}} · 预计总计 ¥{{it.estimated_total}}</h3><p class="small">{{it.reason}}</p><div class="shop-grid"><ShopCard v-for="s in it.stops" :key="s.id" :shop="s" @detail="router.push('/shops/'+s.id)" @buy="buy(s,$event,m.result)"/></div></div><div v-for="c in m.result.business_choices" :key="c.index" class="choice"><span>{{c.index}}. {{c.shop_name}} · {{c.title}}</span><button class="outline" :disabled="busy" @click="send('第'+c.index+'张')">选择这张</button></div><div v-for="o in m.result.personal_orders" :key="o.order_id||o.orderId" class="choice"><span>{{orderTitle(o)}} · {{statusName(o.status)}}</span><button class="text-button" @click="router.push('/orders')">查看订单</button></div><div class="suggestions"><button v-for="r in m.result.relaxations" :key="r.label" :disabled="busy" @click="send(r.message)">{{r.label}}</button></div><details v-if="m.result.candidate_decisions?.length"><summary>筛选与排序依据</summary><p v-for="c in m.result.candidate_decisions" :key="c.shop_id" class="small">{{c.name}}：{{c.reasons.join('；')}}</p></details><button v-if="m.run_id" class="text-button small" @click="showTrace(m.run_id)">本轮运行记录 ↗</button></template></div></article></div><form class="composer" @submit.prevent="send()"><textarea v-model="input" maxlength="500" rows="2" :disabled="busy" aria-label="消息" placeholder="继续聊聊，或者告诉我你的新计划…" @keydown="onComposerKeydown"/><div class="row"><span class="muted small">城市生活助手 · 按需调用点评工具</span><button v-if="busy" type="button" class="outline" @click="stop">停止</button><button v-else class="primary" :disabled="!input.trim()">发送 ↑</button></div></form><div class="footnote">下单和领券需明确确认 · 支付为本地模拟 · Shift+Enter 换行</div></div><ActionDialog v-if="draft" :key="draft.action_id" :draft="draft" @close="draft=null"/><div class="overlay" v-if="trace"><section class="modal wide" role="dialog" aria-modal="true"><button class="close" aria-label="关闭" @click="trace=null">×</button><h2>Agent 运行记录</h2><p class="muted small">{{trace.run_id}} · {{trace.status}} · {{trace.mode}}</p><ol class="trace"><li v-for="(e,i) in trace.events" :key="i"><b>{{e.event}}</b><span>{{e.elapsed_ms}} ms</span><pre>{{JSON.stringify(e,null,2)}}</pre></li></ol></section></div><div class="overlay" v-if="tasks.length"><section class="modal" role="dialog" aria-modal="true"><button class="close" @click="tasks=[]">×</button><h2>任务与恢复</h2><div v-for="t in tasks" :key="t.run_id" class="choice"><span>{{t.run_id}} · {{taskPhase(t.phase)}}</span><button class="outline" :disabled="busy" @click="resume(t.run_id);tasks=[]">{{['completed','resumed'].includes(t.phase)?'查看结果':'恢复'}}</button></div></section></div></template>
