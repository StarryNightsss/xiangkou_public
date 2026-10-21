<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, imageUrl } from './api';
import { useAccount } from './store';
const route = useRoute(), router = useRouter(), account = useAccount();
const blogs = ref<any[]>([]), detail = ref<any>(null), shop = ref<any>(null), feed = ref('hot'), page = ref(1), busy = ref(false), error = ref(''), followed = ref(false), lastId = ref('9223372036854775807'), offset = ref(0), hasMore = ref(false), publishing = ref(false), title = ref(''), content = ref(''), shopId = ref(''), images = ref<string[]>([]), shopOptions = ref<any[]>([]), search = ref('');
const comments = ref<any[]>([]), commentText = ref('');
let generation = 0;
let listLoaded = false;
async function load(append = false) {
    const g = ++generation;
    error.value = '';
    detail.value = null;
    shop.value = null;
    followed.value = false;
    if (!route.params.id && listLoaded && !append) {
        busy.value = false;
        return;
    }
    busy.value = true;
    try {
        if (route.params.id) {
            const b = (await api('/api/community/blogs/' + route.params.id)).data;
            if (g !== generation)
                return;
            detail.value = b;
            if (b?.shopId) {
                const related = await api('/api/catalog/shops/' + b.shopId);
                if (g !== generation)
                    return;
                shop.value = related.shop;
            }
            if (b?.userId && b.userId !== account.user?.id) {
                const status = await api('/api/community/authors/' + b.userId + '/follow');
                if (g !== generation)
                    return;
                followed.value = status.data === true;
            }
            await loadComments(Number(route.params.id));
        }
        else {
            const selectedFeed = feed.value;
            const data = (await api('/api/community/blogs?' + new URLSearchParams({ feed: selectedFeed, page: String(page.value), lastId: lastId.value, offset: String(offset.value) }))).data;
            if (g !== generation)
                return;
            const items = selectedFeed === 'follow' ? (data?.list || []) : (data || []);
            blogs.value = append ? [...blogs.value, ...items] : items;
            listLoaded = true;
            hasMore.value = items.length > 0;
            if (selectedFeed === 'follow') {
                lastId.value = String(data?.minTime ?? lastId.value);
                offset.value = data?.offset ?? 0;
            }
        }
    }
    catch (e) {
        if (g === generation) {
            if (!route.params.id && append)
                page.value = Math.max(1, page.value - 1);
            error.value = (e as Error).message;
        }
    }
    finally {
        if (g === generation)
            busy.value = false;
    }
}
watch(() => route.params.id, () => load(), { immediate: true });
onUnmounted(() => {
    generation++;
});
function changeFeed(value: string) {
    feed.value = value;
    page.value = 1;
    lastId.value = '9223372036854775807';
    offset.value = 0;
    listLoaded = false;
    blogs.value = [];
    load();
}
async function like() {
    if (busy.value || !detail.value)
        return;
    const g = generation, id = detail.value.id;
    busy.value = true;
    try {
        await api(`/api/community/blogs/${id}/like`, 'PUT');
        if (g !== generation)
            return;
        const result = await api('/api/community/blogs/' + id);
        if (g === generation)
            detail.value = result.data;
    }
    catch (e) {
        if (g === generation)
            error.value = (e as Error).message;
    }
    finally {
        if (g === generation)
            busy.value = false;
    }
}
async function loadComments(blogId: number) {
    try {
        const data = await api(`/api/community/blogs/${blogId}/comments`);
        comments.value = data.data || [];
    } catch (e) {
        console.error('加载评论失败', e);
    }
}
function formatTime(value: unknown) {
    if (!value)
        return '';
    const text = String(value).replace('T', ' ');
    return text.length >= 16 ? text.slice(5, 16) : text;
}
async function addComment() {
    if (!commentText.value.trim() || !detail.value) return;
    busy.value = true;
    try {
        await api(`/api/community/blogs/${detail.value.id}/comments`, 'POST', { content: commentText.value.trim() });
        commentText.value = '';
        await loadComments(detail.value.id);
    } catch (e) {
        error.value = (e as Error).message;
    } finally {
        busy.value = false;
    }
}
async function follow() {
    if (busy.value || !detail.value)
        return;
    const g = generation, author = detail.value.userId, enabled = !followed.value;
    busy.value = true;
    try {
        await api(`/api/community/authors/${author}/follow?enabled=${enabled}`, 'PUT');
        if (g === generation)
            followed.value = enabled;
    }
    catch (e) {
        if (g === generation)
            error.value = (e as Error).message;
    }
    finally {
        if (g === generation)
            busy.value = false;
    }
}
async function findShops() {
    try {
        shopOptions.value = (await api('/api/catalog/shops?keyword=' + encodeURIComponent(search.value))).data || [];
    }
    catch (e) {
        error.value = (e as Error).message;
    }
}
async function upload(event: Event) {
    const files = (event.target as HTMLInputElement).files;
    if (!files)
        return;
    busy.value = true;
    try {
        for (const file of Array.from(files)) {
            if (images.value.length >= 9)
                throw new Error('最多9张图片');
            if (file.size > 5000000)
                throw new Error('单张图片最大5MB');
            const data = await new Promise<string>((resolve, reject) => {
                const r = new FileReader();
                r.onload = () => resolve(String(r.result).split(',')[1]);
                r.onerror = reject;
                r.readAsDataURL(file);
            });
            images.value.push((await api('/api/community/images', 'POST', { data })).data);
        }
    }
    catch (e) {
        error.value = (e as Error).message;
    }
    finally {
        busy.value = false;
        (event.target as HTMLInputElement).value = '';
    }
}
async function publish() {
    busy.value = true;
    error.value = '';
    try {
        const data = await api('/api/community/blogs', 'POST', { shopId: Number(shopId.value), title: title.value.trim(), content: content.value.trim(), images: images.value.join(',') });
        publishing.value = false;
        title.value = '';
        content.value = '';
        images.value = [];
        await router.push('/community/' + data.data);
    }
    catch (e) {
        error.value = (e as Error).message;
    }
    finally {
        busy.value = false;
    }
}
async function ask() {
    sessionStorage.setItem('xiangkou-handoff', `请分析${shop.value?.name || '这家店'}（店铺ID ${detail.value.shopId}）的探店口碑，参考笔记ID ${detail.value.id}`);
    await router.push('/chat');
}
</script>
<template><div class="page"><div class="error" v-if="error" role="alert">{{error}}</div><template v-if="!route.params.id"><div class="row"><div><div class="eyebrow">COMMUNITY</div><h2>发现值得去的地方</h2><p class="muted">从真实体验中，找到下一站灵感。</p></div><button class="primary" @click="publishing=true">＋ 发布笔记</button></div><div class="tabs"><button v-for="(name,key) in {hot:'热门',follow:'关注',me:'我的笔记'}" :key="key" :class="{active:feed===key}" :disabled="busy" @click="changeFeed(key)">{{name}}</button></div><div class="blog-grid"><RouterLink v-for="b in blogs" :key="b.id" :to="'/community/'+b.id" class="blog-card"><img v-if="imageUrl(b.images)" :src="imageUrl(b.images)" alt="探店照片" loading="lazy" @error="($event.target as HTMLImageElement).style.display='none'"><div><h3>{{b.title}}</h3><p class="muted small">{{b.content?.slice(0,60)}}</p><div class="row small"><span>{{b.name||'点评用户'}}</span><span>♡ {{b.liked||0}}</span></div></div></RouterLink></div><div v-if="!busy&&!blogs.length" class="empty">{{feed==='follow'?'暂时没有关注动态，去笔记详情关注感兴趣的作者吧。':'暂无探店笔记。'}}</div><div class="pagination"><button class="outline" :disabled="busy||!hasMore" @click="page++;load(true)">加载更多</button></div></template><template v-else-if="detail"><button class="text-button" @click="router.push('/community')">← 返回探店社区</button><div class="blog-detail"><article><h2>{{detail.title}}</h2><div class="row"><button v-if="detail.userId && detail.userId!==account.user.id" class="text-button" @click="router.push('/users/'+detail.userId)">{{detail.name||'点评用户'}}</button><span v-else class="muted">{{detail.name||'点评用户'}}</span><span class="muted">· {{detail.createTime}}</span><button v-if="detail.userId!==account.user.id" class="outline" :disabled="busy" @click="follow">{{followed?'已关注':'关注作者'}}</button></div><div class="photo-grid"><img v-for="path in (detail.images||'').split(',').filter(Boolean)" :key="path" :src="imageUrl(path)" alt="探店照片"></div><p class="blog-text">{{detail.content}}</p><button class="outline" :disabled="busy" @click="like">{{detail.isLike?'已赞 ♥':'点赞 ♡'}} {{detail.liked||0}}</button><div class="comments"><h4>评论 ({{comments.length}})</h4><div v-if="comments.length" class="comment-list"><div v-for="c in comments" :key="c.id" class="comment-item"><button v-if="c.userId!==account.user.id" class="text-button comment-author" @click="router.push('/users/'+c.userId)">{{c.nickName || ('用户' + c.userId)}}</button><span v-else class="comment-author">{{c.nickName || ('用户' + c.userId)}}</span><p>{{c.content}}</p><small class="muted">{{formatTime(c.createTime)}}</small></div></div><div v-else class="muted small">还没有评论，来抢沙发～</div><div class="comment-input"><textarea v-model="commentText" rows="2" placeholder="写下你的评论…" maxlength="500"></textarea><button class="primary small" :disabled="busy||!commentText.trim()" @click="addComment">{{busy?'发表中…':'发表'}}</button></div></div></article><aside v-if="shop" class="related"><div class="eyebrow">关联店铺</div><h3>{{shop.name}}</h3><p class="small muted">{{shop.area}} · {{shop.address}}</p><RouterLink class="text-button" :to="'/shops/'+shop.id">店铺、商品券与探店笔记 →</RouterLink><button class="primary" @click="ask">让助手帮我分析</button></aside></div></template><p v-if="busy" class="muted">正在读取点评数据…</p></div><div class="overlay" v-if="publishing"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="publish-title"><button class="close" :disabled="busy" @click="publishing=false">×</button><h2 id="publish-title">发布探店笔记</h2><form @submit.prevent="publish"><label>标题<input v-model="title" required maxlength="100" placeholder="这次体验怎么样？"></label><label>关联店铺<div class="inline"><input v-model="search" placeholder="搜索店铺名称"><button type="button" class="outline" @click="findShops">查找</button></div><select v-model="shopId" required><option value="">请选择真实店铺</option><option v-for="s in shopOptions" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label>正文<textarea v-model="content" required rows="5" maxlength="10000" placeholder="写下你的真实体验…"/></label><label>图片（最多9张，单张5MB）<input type="file" accept="image/png,image/jpeg,image/webp" multiple :disabled="busy" @change="upload"></label><div class="upload-grid"><div v-for="(path,i) in images" :key="path"><img :src="imageUrl(path)" alt="上传预览"><button type="button" :disabled="busy" @click="images.splice(i,1)">移除</button></div></div><p class="error" v-if="error">{{error}}</p><button class="primary" :disabled="busy||!images.length||!shopId">{{busy?'正在处理…':'发布到探店社区'}}</button></form></section></div></template>
