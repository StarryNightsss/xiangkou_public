// Compile the actual SFC and mount it with Vue's renderer; no browser or network is used.
import {readFileSync} from 'node:fs'
import {createRequire} from 'node:module'
import {fileURLToPath} from 'node:url'
import {randomUUID} from 'node:crypto'
import {runInNewContext} from 'node:vm'
import {parse,compileScript,compileTemplate} from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import {readEvents} from '../src/stream.mjs'

const require=createRequire(import.meta.url)
globalThis.Document??=class Document {}
globalThis.ShadowRoot??=class ShadowRoot {}
const node=(type,text='')=>Object.assign(new EventTarget(),{type,text,children:[],props:{},parent:null,value:'',getRootNode:()=>null})
function insert(child,parent,anchor=null){if(child.parent)remove(child);const at=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(at<0?parent.children.length:at,0,child);child.parent=parent}
function remove(child){if(child.parent){const siblings=child.parent.children;siblings.splice(siblings.indexOf(child),1);child.parent=null}}
const renderer=Vue.createRenderer({
  createElement:type=>node(type),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
  setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},
  patchProp:(n,key,old,value)=>{n.props[key]=value},insert,remove,
  parentNode:n=>n.parent,nextSibling:n=>n.parent?.children[n.parent.children.indexOf(n)+1]||null,
  insertStaticContent(content,parent,anchor){const n=node('#static',content);insert(n,parent,anchor);return[n,n]}
})

export async function flush(){for(let i=0;i<8;i++){await Promise.resolve();await Vue.nextTick()}}
export function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}}
export function find(root,predicate){if(predicate(root))return root;for(const child of root.children){const result=find(child,predicate);if(result)return result}return null}
export function textOf(root){return root.text+root.children.map(textOf).join('')}

export async function mountComponent(name,{api,route=Vue.reactive({params:{},path:'/chat'}),account={},router={},fetch,props={}}={}){
  const filename=fileURLToPath(new URL(`../src/${name}.vue`,import.meta.url))
  const {descriptor}=parse(readFileSync(filename,'utf8'),{filename})
  const script=compileScript(descriptor,{id:`test-${name}`})
  const template=compileTemplate({source:descriptor.template.content,filename,id:`test-${name}`,
    compilerOptions:{bindingMetadata:script.bindings,expressionPlugins:['typescript']}})
  if(template.errors.length)throw new Error(template.errors.join('\n'))
  const navigation={push:async()=>{},replace:async()=>{},...router}
  const mocks={vue:Vue,'vue-router':{useRoute:()=>route,useRouter:()=>navigation},'./store':{useAccount:()=>account},
    './stream.mjs':{readEvents},
    './api':{api,imageUrl:()=>'',statusName:s=>String(s),money:s=>(Number(s||0)/100).toFixed(2)}}
  const context={console,URLSearchParams,AbortController,Event,window:new EventTarget(),crypto:{randomUUID},
    sessionStorage:{getItem:()=>null,removeItem:()=>{}},fetch}
  function evaluate(source){
    const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
    const module={exports:{}}
    const customRequire=id=>id in mocks?mocks[id]:id.endsWith('.vue')?{default:Vue.defineComponent({render:()=>null})}:require(id)
    runInNewContext('(function(require,module,exports){'+js+'\n})',context,{filename})(customRequire,module,module.exports)
    return module.exports
  }
  const component=evaluate(script.content).default
  component.render=evaluate(template.code).render
  const app=renderer.createApp(component,props),root=node('root')
  app.component('RouterLink',{props:['to'],setup:(_, {slots})=>()=>Vue.h('a',slots.default?.())})
  app.mount(root)
  await flush()
  return{state:app._instance.setupState,root,route,unmount:()=>app.unmount()}
}
