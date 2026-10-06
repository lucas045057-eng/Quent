import { render } from '@testing-library/react'
import { vi } from 'vitest'

export const wrap = (data:unknown) => ({schema:'DASHBOARD_API_V1',data,availability:'AVAILABLE',
  observed_at:'2026-09-29T01:00:00Z',sources:['canonical PostgreSQL'],warnings:[]})
export function network(responses:Record<string,unknown>) {
  vi.stubGlobal('fetch',vi.fn(async (url:string) => new Response(JSON.stringify(
    responses[url] ?? wrap(url==='/api/overview'?{mode:'IDLE',metrics:{}}:{items:[]})
  ),{status:200})))
}
export async function app(route:string) {
  window.location.hash='#/'+route
  const {default:App} = await import(/* @vite-ignore */ '../App')
  return render(<App/>)
}
