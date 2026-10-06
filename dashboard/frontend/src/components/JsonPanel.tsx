export default function JsonPanel({value,label='脱敏原始记录'}:{value:unknown;label?:string}) {
  const raw = JSON.stringify(value,null,2)??'null'
  return <details className="raw"><summary>{label}</summary><pre>{raw.slice(0,65536)}</pre>{raw.length>65536&&<p className="inline-note">展示前 64 KiB；来源记录保持完整。</p>}</details>
}
