<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, imageUrl } from './api';

const route = useRoute(), router = useRouter();
const loading = ref(false), error = ref(''), notice = ref('');
const user = ref<any>(null), profile = ref<any>(null), blogs = ref<any[]>([]);
const followed = ref(false), isSelf = ref(false), followBusy = ref(false);
const signDays = ref<number | null>(null), signBusy = ref(false);

function formatTime(value: unknown) {
    if (!value)
        return '';
    const text = String(value).replace('T', ' ');
    return text.length >= 16 ? text.slice(5, 16) : text;
}

async function load() {
    const id = Number(route.params.id);
    error.value = '';
    notice.value = '';
    user.value = null;
    profile.value = null;
    blogs.value = [];
    signDays.value = null;
    if (!Number.isInteger(id) || id <= 0) {
        error.value = '用户 ID 无效';
        return;
    }
    loading.value = true;
    try {
        const data = await api(`/api/community/users/${id}`);
        user.value = data.user;
        profile.value = data.profile || null;
        followed.value = !!data.followed;
        isSelf.value = !!data.is_self;
        blogs.value = data.blogs || [];
        if (isSelf.value) {
            const count = await api('/api/community/sign/count');
            signDays.value = Number(count.data) || 0;
        }
    }
    catch (e) {
        error.value = (e as Error).message;
    }
    finally {
        loading.value = false;
    }
}

async function toggleFollow() {
    if (!user.value || followBusy.value)
        return;
    followBusy.value = true;
    try {
        await api(`/api/community/authors/${user.value.id}/follow?enabled=${!followed.value}`, 'PUT');
        followed.value = !followed.value;
        if (profile.value)
            profile.value.fans = Math.max(0, (profile.value.fans || 0) + (followed.value ? 1 : -1));
    }
    catch (e) {
        error.value = (e as Error).message;
    }
    finally {
        followBusy.value = false;
    }
}

async function signIn() {
    if (signBusy.value)
        return;
    signBusy.value = true;
    try {
        await api('/api/community/sign', 'POST');
        const count = await api('/api/community/sign/count');
        signDays.value = Number(count.data) || 0;
        notice.value = '签到成功';
    }
    catch (e) {
        error.value = (e as Error).message;
    }
    finally {
        signBusy.value = false;
    }
}

watch(() => route.params.id, load, { immediate: true });
</script>

<template>
  <div class="page">
    <div class="error" v-if="error" role="alert">{{ error }}</div>
    <div class="row">
      <button class="outline" @click="router.back()">← 返回</button>
      <h1>个人主页</h1>
    </div>
    <p v-if="loading" class="muted">加载中…</p>
    <template v-else-if="user">
      <section style="border:1px solid #ebe6f4;border-radius:14px;padding:16px;margin:12px 0">
        <div class="row">
          <img v-if="imageUrl(user.icon)" :src="imageUrl(user.icon)" alt="" style="width:56px;height:56px;border-radius:50%;object-fit:cover">
          <span v-else class="badge" style="width:56px;height:56px;display:grid;place-items:center;border-radius:50%">{{ (user.nickName || '?').slice(0, 1) }}</span>
          <div>
            <h2 style="margin:0">{{ user.nickName || ('用户' + user.id) }}</h2>
            <p class="muted small" style="margin:2px 0">
              {{ profile?.city || '未填写城市' }}<span v-if="isSelf"> · 这是你的主页</span>
            </p>
          </div>
        </div>
        <p v-if="profile?.introduce">{{ profile.introduce }}</p>
        <p v-else class="muted small">还没有简介</p>
        <p class="muted small">
          粉丝 {{ profile?.fans ?? 0 }} · 关注 {{ profile?.followee ?? 0 }} · 积分 {{ profile?.credits ?? 0 }}
        </p>
        <div class="row">
          <button v-if="!isSelf" class="primary" :disabled="followBusy" @click="toggleFollow">
            {{ followed ? '已关注' : '关注' }}
          </button>
          <template v-else>
            <button class="primary" :disabled="signBusy" @click="signIn">{{ signBusy ? '签到中…' : '今日签到' }}</button>
            <span class="muted small">连续签到 {{ signDays ?? 0 }} 天</span>
          </template>
        </div>
        <p v-if="notice" class="muted small">{{ notice }}</p>
      </section>
      <h2>TA 的笔记</h2>
      <p v-if="!blogs.length" class="muted small">还没有发布笔记</p>
      <div class="blog-grid" v-else>
        <article class="blog-card" v-for="b in blogs" :key="b.id" @click="router.push('/community/' + b.id)" style="cursor:pointer">
          <h3>{{ b.title }}</h3>
          <p class="muted small">{{ formatTime(b.createTime) }} · 点赞 {{ b.liked || 0 }} · 评论 {{ b.comments || 0 }}</p>
        </article>
      </div>
    </template>
  </div>
</template>
