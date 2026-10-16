/**
 * file-guard 的自检：不起引擎、不起模型，直接跑一遍判定表，把「哪些调用被拒、哪些放行」摆在
 * 眼前。判定本体（src/guard.ts）只依赖 node，所以这里能直 import 它——**改了那张表就跑这个**，
 * 别等某个角色跑到一半才发现围栏漏了一条。
 *
 * 跑法（在引擎根，即 /root/embody-harness）：
 *   ROLE_FILE_GUARD_ALLOWED_ROOTS=<仓库根>/runtime_files \
 *     node --import tsx/esm embody/dsh-plugins/file-guard/check.mts
 * 允许范围取自**同名进程环境变量**（就是 scripts/role-daemon.sh 过桥给角色用的那一个），
 * 不在本文件里另写一份路径 —— 本文件因此不随部署树搬家而失效。
 *
 * 它证明的是「判定函数对不对」，不是「组装对不对」：profile 层那一行有没有打开、bundle 有没有
 * 装进 profile，看 `dsh --profile <角色> --dump-config` 里的 file-guard 行。
 */
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, relative } from 'node:path'
import { judge } from './src/guard.ts'
import { tempRoots } from './src/index.ts'

const allowedRoot = process.env.ROLE_FILE_GUARD_ALLOWED_ROOTS
if (allowedRoot === undefined || allowedRoot.trim() === '') {
  console.error('check: 先设 ROLE_FILE_GUARD_ALLOWED_ROOTS（= configs/paths.py 的 RUNTIME_DIR，'
    + '例如 /root/embody-harness/embody/runtime_files）——本脚本不替你编一个默认根。')
  process.exit(2)
}

/** 项目根 = 允许根的上一条目录（本部署是 <仓库根>/embody）。 */
const projectRoot = dirname(allowedRoot)
const deployment = { cwd: projectRoot, allowedRoots: [allowedRoot, ...tempRoots()] }

/** 一条判例：期望被拒（reason 有值）还是放行（reason 为 undefined）。 */
type Case = readonly [label: string, tool: string, args: unknown, denied: boolean, context: typeof deployment]

const cases: Case[] = [
  ['read 项目根下的源码（configs/paths.py）', 'read', { file_path: join(projectRoot, 'configs/paths.py') }, true, deployment],
  ['read 项目根下的人设文件', 'read', { file_path: join(projectRoot, 'Orchestrator/prompts/orchestrator.md') }, true, deployment],
  ['read 密钥文件（install/secret.env）', 'read', { file_path: join(projectRoot, 'install/secret.env') }, true, deployment],
  ['write 从允许根里 ../.. 绕出去（判真身）', 'write', { file_path: join(allowedRoot, '../../etc/passwd'), content: 'x' }, true, deployment],
  ['read 允许根里的文件', 'read', { file_path: join(allowedRoot, 'tasks/pending_tasks.yaml') }, false, deployment],
  ['read 允许根里还不存在的文件', 'read', { file_path: join(allowedRoot, 'not-there-yet.yaml') }, false, deployment],
  ['write 允许根里', 'write', { file_path: join(allowedRoot, 'scratch.txt'), content: 'x' }, false, deployment],
  ['read 相对路径指向项目根（按会话工作目录解析）', 'read', { file_path: 'configs/paths.py' }, true, deployment],
  ['read 相对路径指向允许根', 'read', { file_path: join(relative(projectRoot, allowedRoot), 'x.yaml') }, false, deployment],
  ['read /etc/passwd（允许根之外一律拒）', 'read', { file_path: '/etc/passwd' }, true, deployment],
  ['read 本机临时目录', 'read', { file_path: join(tmpdir(), 'whatever.txt') }, false, deployment],
  ['read 参数缺失（交给工具自己的 schema 报错）', 'read', {}, false, deployment],
  ['bash（命令行一律拒）', 'bash', { command: 'date' }, true, deployment],
  ['pwsh（命令行一律拒）', 'pwsh', { command: 'Get-Date' }, true, deployment],
  ['glob 不给 path（搜索根 = 会话工作目录）', 'glob', { pattern: '**/*.yaml' }, true, deployment],
  ['glob 指到允许根', 'glob', { pattern: '**/*.yaml', path: allowedRoot }, false, deployment],
  ['grep 指到项目根', 'grep', { pattern: 'x', path: join(projectRoot, 'configs') }, true, deployment],
  ['表外的工具（robot_state）', 'robot_state', { section: 'system' }, false, deployment],
  ['表外的工具（run_code）', 'run_code', { code: 'return 1' }, false, deployment],
]

// 软链接：字面在允许根里、realpath 之后落在别处 —— 判的是真身（与 fs-local 的 resolveLocalTarget 同法）。
const fixture = mkdtempSync(join(tmpdir(), 'file-guard-check-'))
try {
  const fixtureRoot = join(fixture, 'runtime_files')
  mkdirSync(fixtureRoot, { recursive: true })
  writeFileSync(join(fixtureRoot, 'inside.yaml'), 'ok\n')
  symlinkSync('/etc/hostname', join(fixtureRoot, 'escape.yaml'))
  const fixtureContext = { cwd: fixture, allowedRoots: [fixtureRoot, ...tempRoots()] }
  cases.push(
    ['软链接从允许根指到 /etc（判真身）', 'read', { file_path: join(fixtureRoot, 'escape.yaml') }, true, fixtureContext],
    ['允许根里的普通文件', 'read', { file_path: join(fixtureRoot, 'inside.yaml') }, false, fixtureContext],
  )

  let failed = 0
  for (const [label, tool, args, denied, context] of cases) {
    const reason = await judge(tool, args, context)
    const ok = (reason !== undefined) === denied
    if (!ok) failed += 1
    const verdict = reason === undefined ? '放行' : '拒绝'
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${denied ? '该拒' : '该放'}  ${verdict}  ${label}`
      + (reason === undefined ? '' : `  ← ${reason}`))
  }
  console.log(`\n允许根 = ${realpathSync(allowedRoot)}（+ 临时根 ${tempRoots().join('、')}）`)
  console.log(failed === 0 ? `ALL PASS（${cases.length} 条）` : `${failed} FAILED（共 ${cases.length} 条）`)
  process.exitCode = failed === 0 ? 0 : 1
} finally {
  rmSync(fixture, { recursive: true, force: true })
}
