#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runtime_files/ 三个子目录的体积清理 —— 判据与实现都收在这里。

为什么放在 Observer 下
----------------------
Observer/ 是仓库里唯一负责「后台定期照看 runtime_files/」的地方（status_observer 写
robot_status.yaml），这类清理属于同一类职责：不参与业务链路、只维护运行时目录不要无限长。
本模块**不含定时器**：谁要周期跑就自己调（别在这里偷偷起线程）。

谁在跑它
--------
**唯一的调用点是 dsh-plugins/supervisor-loop 的每轮 tick**：插件经 scripts/role-daemon.sh
拿到 configs.paths.RUNTIME_CLEANUP_SCRIPT，起一个 `python3 <本文件> --runtime-sweep --json`
子进程看输出。放在监督器上是因为「目录长胖」是持续发生的事，要在**运行期间**反复看，而不是
开机 / 收尾两个瞬间各看一眼；**不经过大模型**——插件拿到的输出只用来记一行日志，不进上下文、
不产生模型请求。

三条清理共用一个循环
--------------------
`_prune_by_size` 是唯一的删除循环，三个目录的差别只在「哪些文件算可删的」：

1. **logs/**：只删历史日志——轮转下来的 `<组件名>.log.prev`，以及 starter.sh 每次拉起一份的
   `<时间戳>.log`（见 `_is_launch_log`）。正在被进程写的固定名 `<组件名>.log` 删了只是
   unlink——空间要到进程退出才释放，而现场直接没了，所以一律不碰。
2. **tasks/**：只删 `<task_id>.yaml` 已出清单任务的存档（终结的、或停了太久被移出来的，
   一条一份、只增不减）。
   `pending_tasks.yaml` 与它的 `.lock` 是编排器每轮在读的活任务板，按文件名**显式排除**——
   不靠 glob 形状，免得将来有人给存档换个后缀就把活文件捎带上。
3. **photos/**：只删 frame_producer 落盘的受管图。**「哪些文件名算我们落的」这条判据不在这里**
   ——它属于 `skills/camera/frame_producer.py`（全仓图像落盘的独占管理者），本模块调它的
   `is_managed_photo()`。两份判据一旦分叉就会「擦肩而过」：写手认的文件这里不认（越攒越多），
   或反过来（把人工放进目录的图误删）。写手那边**不再自己清**（`save_photo` 只写不清）。

**两档水位，不是贴着单一上限删**（configs 的 `RUNTIME_*_TRIGGER_MB` / `_TARGET_MB`）：体积超过
TRIGGER 才动手，一动手就删到 `<= TARGET`。留出这段落差是刻意的——删到「刚好不超」之后，下一轮
只要再长一点就又触发，于是每轮都删一点；有了落差就能一次删够、之后安静一阵。

**绝不越权删**：体积算的是整个目录的顶层常规文件（就是用户 `du` 看到的那个数），删的只是上面
那些可删的文件。删光候选仍超 TARGET 就此停手（目录里还混着活文件或别人的大文件），调用方据
返回值之外的信息报这件事。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

# 仓库根（本文件 Observer/ 上一级）入 sys.path，以便 import configs / skills
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (  # noqa: E402
    RUNTIME_LOGS_TARGET_MB,
    RUNTIME_LOGS_TRIGGER_MB,
    RUNTIME_PHOTOS_TARGET_MB,
    RUNTIME_PHOTOS_TRIGGER_MB,
    RUNTIME_TASKS_TARGET_MB,
    RUNTIME_TASKS_TRIGGER_MB,
)
from configs.paths import (  # noqa: E402
    RUNTIME_LOGS_DIR,
    RUNTIME_PENDING_TASKS_FILE,
    RUNTIME_PHOTOS_DIR,
    RUNTIME_TASKS_DIR,
)
from skills.camera import frame_producer  # noqa: E402


def _dir_bytes(directory: Path) -> int:
    """目录**顶层**常规文件的大小之和。

    不递归子目录：受管的这三个目录都是平铺的，子目录有各自的归属
    （photos/scratch 归 frame_producer）。读不动的条目跳过——清理是旁路，绝不抛。
    """
    total = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _prune_by_size(
    directory: Path,
    trigger_bytes: int,
    target_bytes: int,
    eligible: Callable[[str], bool],
    *,
    dry_run: bool = False,
) -> List[Path]:
    """目录超过 trigger_bytes 时，把 eligible() 认的文件按 mtime 从旧到新删，直到 <= target_bytes。

    - **没到触发线一个都不删**（返回空列表）；`trigger_bytes <= 0` = 不清理。
    - 体积算的是整个目录（见 `_dir_bytes`），删的只是 eligible 认的那些：目录里混着别的大文件
      时可能删光候选仍超目标，那时**就此停手**、不越权删别的。
    - 新旧按 **mtime** 排：logs 的 `.log.prev` 由轮转 `replace` 产生，mtime 就是轮转时刻；
      tasks 的存档是一次性写入；photos 的文件名本身即时间序（与 mtime 同序）。
    - `dry_run=True` 只算不删，返回「本来会删的」那些。
    - 目录不存在 / 读不动 → 返回空列表，绝不抛。
    """
    if trigger_bytes <= 0:
        return []
    remaining = _dir_bytes(directory)
    if remaining <= trigger_bytes:
        return []
    candidates: List[Tuple[float, str, int]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False) or not eligible(entry.name):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    candidates.append((stat.st_mtime, entry.name, stat.st_size))
                except OSError:
                    continue
    except OSError:
        return []
    candidates.sort()  # (mtime, 名字) —— 旧→新
    doomed: List[Path] = []
    for _mtime, name, size in candidates:
        if remaining <= target_bytes:
            break
        doomed.append(directory / name)
        remaining -= size
    if dry_run:
        return doomed
    removed: List[Path] = []
    for path in doomed:
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def _is_rotated_log(name: str) -> bool:
    """轮转下来的历史日志：`<组件名>.log.prev`。

    **正在写的 `<name>.log` 不算**——进程持有它的 fd，删了只是 unlink：空间要到进程退出
    才释放，而现场直接没了。
    """
    return name.endswith('.log.prev') and len(name) > len('.log.prev')


# starter.sh 每次拉起一份的终端日志：`<YYYYmmdd-HHMMSS>.log`。格式的唯一出处是
# scripts/lib-starter.sh 的 starter_start_terminal_log，那里改一处这里要跟着改。
_LAUNCH_LOG_RE = re.compile(r'\d{8}-\d{6}\.log')


def _is_launch_log(name: str) -> bool:
    """每次拉起一份的终端日志：`<YYYYmmdd-HHMMSS>.log`。

    与上面那种 `.prev` 的差别是它**不改名**：不是轮转出来的，一次启动一份，唯一会把它挪走的
    就是本模块。**正在写的那一份（本次拉起的）也在这个形状里**，它靠 mtime 最新排在删除序列的
    最后：只有把别的候选删完仍超目标线（单份日志自己就长过目标线）才会碰到它，那之后它只会
    被 unlink——空间到 starter.sh 退出才释放。
    """
    return _LAUNCH_LOG_RE.fullmatch(name) is not None


def _is_prunable_log(name: str) -> bool:
    """logs/ 里算「历史」的两类文件：轮转下来的 `.prev`、每次拉起一份的 `<时间戳>.log`。

    固定名 `<组件名>.log`（进程持有 fd、正在写）两类都不匹配，这是本模块「不碰活文件」那条
    规矩的落点。
    """
    return _is_rotated_log(name) or _is_launch_log(name)


def _is_task_archive(name: str) -> bool:
    """已终结任务的存档：`<task_id>.yaml`（判据见 configs/paths.py 的 runtime_task_file）。

    `pending_tasks.yaml` 是**活的任务板**（编排器每轮在读），不是历史，按文件名显式排除——
    不靠 glob 形状，免得将来有人给存档换个后缀就把活文件捎带上。
    """
    return name.endswith('.yaml') and name != RUNTIME_PENDING_TASKS_FILE.name


def prune_logs(
    directory: Path = RUNTIME_LOGS_DIR,
    trigger_bytes: int = RUNTIME_LOGS_TRIGGER_MB * 1024 * 1024,
    target_bytes: int = RUNTIME_LOGS_TARGET_MB * 1024 * 1024,
    *,
    dry_run: bool = False,
) -> List[Path]:
    """logs/ 超过触发线就删最旧的历史日志（`.prev` 与每次拉起一份的 `<时间戳>.log`），返回删掉的路径（旧→新）。

    两档水位缺省＝`configs.RUNTIME_LOGS_TRIGGER_MB` / `_TARGET_MB`。**只删历史**（判据见
    `_is_prunable_log`）：正在被进程写的固定名 `.log` 一律不碰（理由见 `_is_rotated_log`）。
    轮转本身（start_agent.py / starter.sh）已经把每个流限死在两份；每次拉起那份则要靠这条
    兜底——它是一次一份、不轮转的，不删就没有上限（本次拉起的那份排在最末，见 `_is_launch_log`）。
    """
    return _prune_by_size(directory, trigger_bytes, target_bytes, _is_prunable_log, dry_run=dry_run)


def prune_task_archives(
    directory: Path = RUNTIME_TASKS_DIR,
    trigger_bytes: int = RUNTIME_TASKS_TRIGGER_MB * 1024 * 1024,
    target_bytes: int = RUNTIME_TASKS_TARGET_MB * 1024 * 1024,
    *,
    dry_run: bool = False,
) -> List[Path]:
    """tasks/ 超过触发线就删最旧的 `<task_id>.yaml` 存档，返回删掉的路径（旧→新）。

    两档水位缺省＝`configs.RUNTIME_TASKS_TRIGGER_MB` / `_TARGET_MB`。`pending_tasks.yaml`
    与 `pending_tasks.yaml.lock` **永不删**（判据见 `_is_task_archive`）。

    删掉的存档全仓只有 `plugin_tools/task_board.py` 生成新任务号时查一下「存不存在」防重号，
    没有任何地方读回内容；任务号是 `<前缀>-<epoch 秒>` 单调的，删掉最旧的不会撞车。
    """
    return _prune_by_size(directory, trigger_bytes, target_bytes, _is_task_archive, dry_run=dry_run)


def prune_photos(
    directory: Path = RUNTIME_PHOTOS_DIR,
    trigger_bytes: int = RUNTIME_PHOTOS_TRIGGER_MB * 1024 * 1024,
    target_bytes: int = RUNTIME_PHOTOS_TARGET_MB * 1024 * 1024,
    *,
    dry_run: bool = False,
) -> List[Path]:
    """photos/ 超过触发线就删最旧的受管图，返回删掉的路径（旧→新）。

    两档水位缺省＝`configs.RUNTIME_PHOTOS_TRIGGER_MB` / `_TARGET_MB`。可删的判据来自
    `frame_producer.is_managed_photo()`（见模块注释第 3 条）：目录里人工放的图、以及
    `scratch/` 抓拍临时目录（在子目录里，本来就不在顶层）都不碰。
    """
    return _prune_by_size(
        directory, trigger_bytes, target_bytes, frame_producer.is_managed_photo, dry_run=dry_run
    )


# 报告用的一张表：(标签, 目录, 触发线 MB, 目标线 MB)。**不参与判断**——各区间的实际取值由上面
# 三个函数的缺省参数单向取自 configs/configs.py；这张表只负责把同样的数印出来给人看。
_REPORT_TARGETS = (
    ('logs', RUNTIME_LOGS_DIR, RUNTIME_LOGS_TRIGGER_MB, RUNTIME_LOGS_TARGET_MB),
    ('tasks', RUNTIME_TASKS_DIR, RUNTIME_TASKS_TRIGGER_MB, RUNTIME_TASKS_TARGET_MB),
    ('photos', RUNTIME_PHOTOS_DIR, RUNTIME_PHOTOS_TRIGGER_MB, RUNTIME_PHOTOS_TARGET_MB),
)


def sweep_runtime_files(*, dry_run: bool = False) -> Dict[str, List[Path]]:
    """三个目录各清一轮，返回 `{目录名: 删掉的路径}`（`dry_run=True` 时是「本来会删的」）。

    这是对外的唯一入口：`--runtime-sweep` 与监督器插件都走它。各目录的触发/目标线、可删名单
    全在各自的 `prune_*` 里，这里只负责跑齐三个、把结果按目录分好。
    """
    return {
        'logs': prune_logs(dry_run=dry_run),
        'tasks': prune_task_archives(dry_run=dry_run),
        'photos': prune_photos(dry_run=dry_run),
    }


def _mb(size: int) -> str:
    return f'{size / 1024 / 1024:.1f}MB'


def _report_sweep(dry_run: bool) -> int:
    """`--runtime-sweep` 的人读报告：逐目录一行结论，删了谁就列出来。"""
    removed = sweep_runtime_files(dry_run=dry_run)
    verb = '将删除' if dry_run else '已删除'
    for label, directory, trigger_mb, target_mb in _REPORT_TARGETS:
        doomed = removed[label]
        size = _dir_bytes(directory)
        if not doomed:
            print(f'[cleanup] {directory} 无需清理（触发线 {trigger_mb}MB，当前 {_mb(size)}）')
            continue
        print(f'[cleanup] {verb} {len(doomed)} 个（{label}，触发线 {trigger_mb}MB '
              f'→ 目标 {target_mb}MB）：')
        for path in doomed:
            print(f'  {path}')
        if not dry_run and size > target_mb * 1024 * 1024:
            print(f'[cleanup] 注意：{directory} 删完仍超目标线——目录里还有不能删的文件'
                  f'（活的 pending_tasks / 进程正在写的 .log / 别人放进来的图）。')
    return 0


def _json_payload(dry_run: bool) -> Dict[str, object]:
    """`--json` 的载荷：给监督器插件一行可解析的结论（它在真删了东西时才记一行日志）。"""
    removed = sweep_runtime_files(dry_run=dry_run)
    return {
        'dryRun': dry_run,
        'dirs': {
            label: {
                'bytes': _dir_bytes(directory),
                'removed': [str(path) for path in removed[label]],
            }
            for label, directory, _trigger_mb, _target_mb in _REPORT_TARGETS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description='按体积清理 runtime_files/ 下的旧文件：logs/ tasks/ photos/ 三个目录，两档水位',
    )
    parser.add_argument(
        '--runtime-sweep', action='store_true', required=True,
        help='清一轮：三个目录各自超过 configs 的 RUNTIME_*_TRIGGER_MB 就删最旧的可删文件，'
             '删到 <= RUNTIME_*_TARGET_MB 为止。没有别的模式——不带这个开关等于没说要清什么',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='只列出会删哪些文件，不真删',
    )
    parser.add_argument(
        '--json', action='store_true',
        help='把结论打成一行 JSON（给监督器插件解析），不出人读的报告',
    )
    args = parser.parse_args()

    if args.json:
        print(json.dumps(_json_payload(args.dry_run), ensure_ascii=False))
        return 0
    return _report_sweep(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
