export async function readEvents(response, onEvent) {
  if (!response.body) throw new Error('流式连接不可用')
  const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='',result=null
  try {
    while (true) {
      const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done})
      let index
      while((index=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,index);buffer=buffer.slice(index+2);const raw=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');if(!raw)continue;const item=JSON.parse(raw);onEvent(item);if(item.type==='result')result=item.data;if(item.type==='error')throw new Error(item.detail||'运行失败');if(item.type==='cancelled')throw new Error('已停止生成')}
      if(done)break
    }
    if(!result)throw new Error('连接中断，请重新发送')
    return result
  } finally {reader.releaseLock()}
}
