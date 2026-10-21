export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await fetch(path, { method, credentials: 'same-origin', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  // 401/502 可能返回空体或 HTML，先按文本读，解析失败时回退到状态码提示，避免抛出 SyntaxError。
  const text = await r.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { data = null }
  if (!r.ok) {
    if (r.status === 401) window.dispatchEvent(new Event('auth-expired'))
    const detail = data && typeof data.detail === 'string' ? data.detail : ''
    throw new Error(detail || (r.status === 401 ? '登录已失效，请重新登录' : `请求失败（HTTP ${r.status}），请稍后重试`))
  }
  return data as T
}
export function imageUrl(value: unknown): string {
  const path = String(value || '').split(',')[0]
  const origin = import.meta.env.VITE_DIANPING_ASSET_ORIGIN || location.origin
  if (/^\/imgs\/[\w/.-]+$/.test(path)) return `${origin}${path}`
  if (/^\/(blogs|types|icons)\/[\w/.-]+$/.test(path)) return `${origin}/imgs${path}`
  if (/^https?:\/\//.test(path)) return path
  return ''
}
export const money = (cents: unknown) => (Number(cents || 0) / 100).toFixed(2)
export const statusName = (status: unknown) => ({1:'待支付',2:'已支付 / 可使用',3:'已核销',4:'已取消'}[Number(status)] || '处理中')
