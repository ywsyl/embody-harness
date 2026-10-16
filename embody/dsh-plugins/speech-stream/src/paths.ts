/**
 * 本插件要用的三个仓库内路径。
 *
 * 与 dsh-plugins/tools-bridge 同一做法：仓库根从**模块位置**推出（本包位于
 * `<embody>/dsh-plugins/speech-stream/{src,lib}/`，源码装载与 lib 装载上溯三级都一样），
 * 因此它们是常量而不是配置项——同一个仓库的每份部署，这几个路径都一样。真正随部署变的是
 * 节拍与阈值，那些走 Config。
 * @module @embody/dsh-speech-stream/paths
 */

import { fileURLToPath } from 'node:url'

/** 仓库根（= 本包的 `embody/`），子进程的工作目录也用它。 */
export const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url))

/** TTS 播报脚本（**回退路径**：服务连不上时每句一个进程；ROS 环境由脚本自己 source）。 */
export const TTS_SCRIPT = `${REPO_ROOT}skills/tts-speak/scripts/run_tts_speak.sh`

/**
 * 常驻 TTS 服务的 unix socket（`TTS_Manager/tts_service.py --serve` 的 bind 点）。
 * 与 Python 侧 `configs/paths.py` 的 `TTS_SERVICE_SOCKET` 是同一个文件（`runtime_files/` 下，
 * 不进 Config：它由仓库位置决定，见模块注释）。连不上就当服务不在，整路退回逐句 spawn。
 */
export const TTS_SERVICE_SOCKET = `${REPO_ROOT}runtime_files/tts_speak.sock`

/**
 * 「这一轮已经播到第几个字」的记录文件，**单写者 = 本插件**。
 * `plugin_tools/audio_tools.py` 只在派发 `speak` / `speak_listen` 时读它去重，不写；
 * 因此这里用 tmp+rename 的原子写就够，不需要跨进程锁。
 */
export const STATE_FILE = `${REPO_ROOT}runtime_files/spoken_stream.json`
