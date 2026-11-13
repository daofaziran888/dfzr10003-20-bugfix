/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export type ActionOutcome = { ok: boolean; message: string; entry?: Record<string, unknown> | null }

/** 动作类 POST：业务失败（ok=false）时把后端给出的可读原因带回来，而不是统一报“操作失败”。 */
export async function postAction(path: string, values: Record<string, unknown>): Promise<ActionOutcome> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // 非 JSON 错误体时保留默认说明
    }
    return { ok: false, message: detail }
  }
  return (await response.json()) as ActionOutcome
}
