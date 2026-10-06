import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'

function files(dir) {
  return readdirSync(dir,{withFileTypes:true}).flatMap(item=>item.isDirectory()?files(join(dir,item.name)):[join(dir,item.name)])
}
const source = [...files('src'),...files('scripts'),'package.json','package-lock.json','index.html','tsconfig.json','vite.config.ts']
  .filter(path=>!path.endsWith('.sha256')).sort()
const hashes = paths => Object.fromEntries(paths.map(path=>[path.replaceAll('\\','/'),createHash('sha256').update(readFileSync(path)).digest('hex')]))
const stamp = {schema:'QUANT_DASHBOARD_BUILD_V1',source_files:hashes(source),
  assets:hashes(files('dist').filter(path=>!path.endsWith('build-stamp.json')))}
writeFileSync('dist/build-stamp.json',JSON.stringify(stamp,null,2)+'\n')
process.stdout.write('Source and asset hashes recorded.\n')
