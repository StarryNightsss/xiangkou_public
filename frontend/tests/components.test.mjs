import test from 'node:test'
import assert from 'node:assert/strict'
import {reactive} from 'vue'
import {mountComponent,flush,deferred,find,textOf} from './component-harness.mjs'

test('returning from a followed blog preserves loaded pages and its next cursor',async()=>{
  const calls=[],route=reactive({params:{},path:'/community'})
  const api=async path=>{
    calls.push(path)
    if(path==='/api/community/blogs/1')return{data:{id:1,userId:7}}
    if(path.includes('feed=follow'))return{data:{list:[{id:1,title:'第一篇'},{id:2,title:'第二篇'}],minTime:100,offset:1}}
    return{data:[]}
  }
  const c=await mountComponent('Community',{api,route,account:{user:{id:7}}})
  try{
    c.state.changeFeed('follow');await flush()
    assert.deepEqual(Array.from(c.state.blogs,b=>b.id),[1,2])
    route.params.id='1';await flush();delete route.params.id;await flush()
    assert.deepEqual(Array.from(c.state.blogs,b=>b.id),[1,2])
    assert.equal(calls.filter(p=>p.includes('feed=follow')).length,1)
    assert.equal(c.state.lastId,'100');assert.equal(c.state.offset,1)
  }finally{c.unmount()}
})

test('late shop and follow responses cannot overwrite a different blog',async()=>{
  for(const delayAt of ['shop','follow']){
    const slow=deferred(),route=reactive({params:{id:'1'},path:'/community/1'})
    const api=async path=>{
      if(path.startsWith('/api/community/blogs/')){const id=Number(path.split('/').at(-1));return{data:{id,shopId:id,userId:id+10}}}
      if(path==='/api/catalog/shops/1')return delayAt==='shop'?slow.promise:{shop:{id:1}}
      if(path==='/api/community/authors/11/follow')return slow.promise
      if(path==='/api/catalog/shops/2')return{shop:{id:2}}
      if(path==='/api/community/authors/12/follow')return{data:false}
      throw new Error(path)
    }
    const c=await mountComponent('Community',{api,route,account:{user:{id:7}}})
    try{
      route.params.id='2';await flush()
      slow.resolve(delayAt==='shop'?{shop:{id:1}}:{data:true});await flush()
      assert.equal(c.state.detail.id,2);assert.equal(c.state.shop.id,2);assert.equal(c.state.followed,false)
    }finally{c.unmount()}
  }
})

test('composer ignores IME confirmation but sends an ordinary Enter',async()=>{
  let requests=0
  const c=await mountComponent('Chat',{api:async()=>({}),account:{refreshSessions:async()=>{}},fetch:async()=>{requests++;throw new Error('mock end')}})
  try{
    c.state.input='想找一家'
    const textarea=find(c.root,n=>n.type==='textarea')
    let prevented=0
    const key={key:'Enter',isComposing:true,preventDefault(){prevented++}}
    textarea.props.onKeydown(key);await flush()
    assert.equal(requests,0);assert.equal(prevented,0);assert.equal(c.state.input,'想找一家')
    textarea.props.onKeydown({...key,isComposing:false,keyCode:229});await flush()
    assert.equal(requests,0)
    textarea.props.onKeydown({...key,isComposing:false,keyCode:13});await flush()
    assert.equal(requests,1);assert.equal(prevented,1)
  }finally{c.unmount()}
})

test('session list failure preserves a successful assistant answer and cards',async()=>{
  const result={answer:'已核实的回答',run_id:'run',session_id:'session',shops:[],personal_orders:[{order_id:'88',title:'真实商品标题',status:1}]}
  const c=await mountComponent('Chat',{api:async()=>({}),account:{refreshSessions:async()=>{throw new Error('list unavailable')}},
    fetch:async()=>new Response(`data: ${JSON.stringify({type:'result',data:result})}\n\n`)})
  try{
    await c.state.send('找火锅');await flush()
    assert.equal(c.state.messages.at(-1).text,result.answer)
    assert.equal(c.state.messages.at(-1).result.run_id,'run')
    assert.match(c.state.error,/会话列表刷新失败/)
    assert.match(textOf(c.root),/真实商品标题/)
  }finally{c.unmount()}
})

test('recovering another session replaces transcript, context and action preview',async()=>{
  const result={answer:'B恢复结果',session_id:'B',run_id:'runB',recommendation_run_id:'recommendationB',shops:[],action_draft:{action_id:'draftB'}}
  const api=async path=>{
    if(path==='/api/tasks/taskB/resume')return result
    if(path==='/api/sessions/B/messages')return{messages:[{id:'B-user',role:'user',text:'B原问题'}]}
    throw new Error(path)
  }
  const c=await mountComponent('Chat',{api,account:{refreshSessions:async()=>{}}})
  try{
    c.state.messages=[{id:'A-user',role:'user',text:'A原问题'}];c.state.session='A';c.state.context='recommendationA'
    await c.state.resume('taskB');await flush()
    assert.deepEqual(Array.from(c.state.messages,m=>m.text),['B原问题','B恢复结果'])
    assert.equal(c.state.context,'recommendationB');assert.equal(c.state.session,'B')
    assert.equal(c.state.messages.at(-1).run_id,'runB');assert.equal(c.state.draft.action_id,'draftB')
  }finally{c.unmount()}
})

test('viewing an already saved task does not append the same answer twice',async()=>{
  const result={answer:'原回答',session_id:'B',run_id:'runB',shops:[]}
  const c=await mountComponent('Chat',{api:async path=>path.includes('/resume')?result:{messages:[{id:'answer',role:'assistant',text:'原回答',run_id:'runB'}]},account:{refreshSessions:async()=>{}}})
  try{await c.state.resume('taskB');assert.equal(c.state.messages.length,1)}finally{c.unmount()}
})

test('preflight error allows explicit retry only after server confirms prepared state',async()=>{
  let confirms=0
  const c=await mountComponent('ActionDialog',{props:{draft:{action_id:'draft',voucher:{pay_cents:100}}},api:async path=>{
    if(path.endsWith('/confirm')){confirms++;throw new Error('预检暂不可用')}
    return{action_id:'draft',state:'prepared',order_id:null}
  }})
  try{
    await c.state.confirm();await flush()
    assert.equal(c.state.submitted,false);assert.match(c.state.error,/预检暂不可用/)
    await c.state.confirm();assert.equal(confirms,2)
  }finally{c.unmount()}
})

test('unknown submission result keeps confirmation disabled',async()=>{
  const c=await mountComponent('ActionDialog',{props:{draft:{action_id:'draft'}},api:async path=>{
    if(path.endsWith('/confirm'))throw new Error('网络中断')
    return{action_id:'draft',state:'uncertain',order_id:null}
  }})
  try{await c.state.confirm();assert.equal(c.state.submitted,true)}finally{c.unmount()}
})
