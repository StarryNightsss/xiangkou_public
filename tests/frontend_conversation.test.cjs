const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { webcrypto } = require('node:crypto');
function setup() {
  const context = { window: {}, crypto: webcrypto };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/conversation.js'), 'utf8'), context);
  const records = new Map();
  const storage = { getItem: key => records.get(key), setItem: (key, value) => records.set(key, value), removeItem: key => records.delete(key) };
  return { Conversation: context.window.巷口Conversation, storage, records };
}
test('two turns append and update only their own assistant message', () => {
  const { Conversation, storage } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner:session');
  chat.add('user', '两人吃火锅'); const first = chat.add('assistant', '查询中', 'pending');
  chat.update(first, '第一轮方案'); chat.add('user', '预算降到200');
  const second = chat.add('assistant', '查询中', 'pending'); chat.update(second, '第二轮方案');
  assert.equal(chat.entries.length, 4); assert.equal(chat.entries[1].text, '第一轮方案');
  assert.equal(chat.entries[3].text, '第二轮方案');
});
test('account and session switches isolate transcripts', () => {
  const { Conversation, storage } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner7:session1'); chat.add('user', '自己的消息');
  chat.switchTo('owner8:session1'); assert.equal(chat.entries.length, 0);
  chat.switchTo('owner7:session2'); assert.equal(chat.entries.length, 0);
  chat.switchTo('owner7:session1'); assert.equal(chat.entries[0].text, '自己的消息');
  chat.switchTo(null); assert.equal(chat.entries.length, 0);
});
test('reload turns an unfinished request into an interrupted message', () => {
  const { Conversation, storage } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner:session'); chat.add('assistant', '正在查询', 'pending');
  const restored = new Conversation(storage); restored.switchTo('owner:session');
  assert.equal(restored.entries[0].status, 'interrupted');
  assert.match(restored.entries[0].text, /中断/);
});
test('first response binds draft messages to the server session', () => {
  const { Conversation, storage, records } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner:draft'); chat.add('user', '找火锅'); chat.bind('owner:session');
  assert.equal(records.has('owner:draft'), false);
  const restored = new Conversation(storage); restored.switchTo('owner:session');
  assert.equal(restored.entries[0].text, '找火锅');
});
test('storage is bounded and secrets are masked', () => {
  const { Conversation, storage } = setup(); const chat = new Conversation(storage); chat.switchTo('owner:session');
  for (let n = 0; n < 205; n++) chat.add('user', '消息'+n);
  assert.equal(chat.entries.length, 200);
  assert.equal(JSON.parse(storage.getItem('owner:session')).length, 40);
  chat.add('user', 'sk-abcdefghijklmnop 13900009999 test@example.com');
  assert.equal(chat.entries.at(-1).text.includes('abcdefghijklmnop'), false);
  assert.equal(chat.entries.at(-1).text.includes('13900009999'), false);
  assert.equal(chat.entries.at(-1).text.includes('test@example.com'), false);
});
test('clear removes the current transcript', () => {
  const { Conversation, storage, records } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner:session'); chat.add('assistant', 'done'); chat.clear();
  assert.equal(records.has('owner:session'), false); assert.equal(chat.entries.length, 0);
});

test('server transcript restores older turns and preserves completed status', () => {
  const { Conversation, storage } = setup(); const chat = new Conversation(storage);
  chat.switchTo('owner:session'); chat.add('assistant', 'stale local');
  chat.replace(Array.from({length: 120}, (_, n) => ({id: 'server-'+n, role: n%2 ? 'assistant':'user', text: 'round'+n, status:'done'})));
  assert.equal(chat.entries.length, 120); assert.equal(chat.entries[0].id, 'server-0');
  assert.equal(chat.entries.at(-1).text, 'round119');
  chat.replace([{id:'cancel',role:'assistant',text:'已取消',status:'cancelled'}]);
  assert.equal(chat.entries[0].status, 'cancelled');
});
