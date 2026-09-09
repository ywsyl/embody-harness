/**
 * @embody/dsh-rolebus —— 里程碑 0 占位实现。
 *
 * 本项目里各角色（interactor / orchestrator / supervisor ...）是各自独立的 dsh
 * profile/进程；它们之间的结构化事件（events.md 为唯一事实源）需要一条跨进程消息
 * 总线。这个包就是总线的载体：里程碑 0 只验证“bundle 能被 profile 引入、其 config
 * 能在配置分层中被逐角色改写并正确落盘”；真正的收发实现放到里程碑 1。
 *
 * 遵循 dsh 引擎的函数插件约定：只命名导出 name / apply（必要），后续再按需加
 * inject / Config；绝不默认导出（默认导出会被 Loader 丢弃 inject 等元信息）。
 *
 * @module @embody/dsh-rolebus
 */
export const name = 'rolebus'

/**
 * 占位 apply。里程碑 0 不挂载任何运行能力，仅保证插件可被正常加载；
 * 里程碑 1 在这里通过 ctx.effect()/ctx.on() 注册总线收发与路由。
 * @param ctx - Cordis registrant context（本里程碑未注入服务，仅作签名占位）。
 */
export function apply(ctx) {
  void ctx
}
