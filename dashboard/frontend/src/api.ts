import { useEffect, useState } from 'react'
import type { ApiState, Envelope } from './types'

export function usePolling<T>(url:string|null, interval=3000):ApiState<T> {
  const [state,setState] = useState<ApiState<T>>({response:null,loading:!!url,error:null,stale:false})
  useEffect(() => {
    let stopped = false
    let timer:ReturnType<typeof setTimeout>|undefined
    let controller:AbortController|undefined
    setState({response:null,loading:!!url,error:null,stale:false})
    if (!url) return
    async function refresh() {
      controller = new AbortController()
      const timeout = setTimeout(() => controller?.abort(),5000)
      try {
        const result = await fetch(url!,{signal:controller.signal,cache:'no-store'})
        if (!result.ok) throw new Error('API_UNAVAILABLE')
        const body = await result.json() as Envelope<T>
        if (body.schema !== 'DASHBOARD_API_V1' || !('data' in body) || !Array.isArray(body.sources) || !Array.isArray(body.warnings)) throw new Error('INVALID_API_RESPONSE')
        if (!stopped) setState({response:body,loading:false,error:null,stale:false})
      } catch {
        if (!stopped) setState(previous => ({...previous,loading:false,error:'无法连接本地 API，请检查后台进程。',stale:!!previous.response}))
      } finally {
        clearTimeout(timeout)
        if (!stopped && interval > 0) timer = setTimeout(refresh,interval)
      }
    }
    void refresh()
    return () => { stopped=true; clearTimeout(timer); controller?.abort() }
  },[url,interval])
  return state
}
