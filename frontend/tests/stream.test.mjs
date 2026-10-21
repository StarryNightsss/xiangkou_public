import test from 'node:test'
import assert from 'node:assert/strict'
import {readEvents} from '../src/stream.mjs'
function response(text,chunks){const bytes=new TextEncoder().encode(text);return new Response(new ReadableStream({start(c){let i=0;for(const n of chunks){c.enqueue(bytes.slice(i,i+n));i+=n}c.enqueue(bytes.slice(i));c.close()}}))}
test('split UTF-8 frames and heartbeat retain authoritative result',async()=>{const events=[];const r=await readEvents(response(': heartbeat\n\ndata: {"type":"event","data":{"event":"tool_start"}}\n\ndata: {"type":"result","data":{"answer":"你好"}}\n\n',[3,9,15,36,51]),e=>events.push(e));assert.equal(r.answer,'你好');assert.equal(events.length,2)})
test('EOF without result reports interruption',async()=>{await assert.rejects(readEvents(response(': heartbeat\n\n',[]),()=>{}),/连接中断/)})
test('terminal error cannot look like a successful assistant answer',async()=>{await assert.rejects(readEvents(response('data: {"type":"error","detail":"服务不可用"}\n\n',[9]),()=>{}),/服务不可用/)})
