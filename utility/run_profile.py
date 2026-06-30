"""
run_profile.py
実行プロファイル（シミュレーション時間・命令数）を JSON に保存・取得する。
Sniper 版: sim.stats.sqlite3 / sim.info から読む。
"""

import json
import os
import tempfile
import threading

_lock = threading.Lock()

PROFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "Documents", "Data", "run_profile.json"
)
PROFILE_PATH = os.path.normpath(PROFILE_PATH)


def _load() -> dict:
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH) as f:
        content = f.read()
    return json.loads(content) if content.strip() else {}


def _save(profile: dict) -> None:
    os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
    dir_ = os.path.dirname(PROFILE_PATH)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tf:
        json.dump(profile, tf, indent=2)
        tmp_path = tf.name
    os.replace(tmp_path, PROFILE_PATH)


def _key(workload: str, bench_class: str, num_threads: int) -> str:
    return f"{workload}_{bench_class}_{num_threads}"


def get_reference(workload: str, bench_class: str, num_threads: int) -> dict | None:
    """{"simTime": float, "instructions": int, "wallTime": float} または None。"""
    with _lock:
        return _load().get(_key(workload, bench_class, num_threads))


def update_from_run(
    workload: str,
    bench_class: str,
    num_threads: int,
    output_dir: str,
    wall_time: float,
) -> None:
    """Sniper の出力から統計を読んでプロファイルを更新する。"""
    from utility.stats_reader import parse_sim_time, parse_instructions

    sim_time     = parse_sim_time(output_dir)
    instructions = parse_instructions(output_dir)
    total_insts  = sum(instructions.values()) if instructions else 0

    if sim_time is None:
        return

    with _lock:
        profile = _load()
        key = _key(workload, bench_class, num_threads)
        existing = profile.get(key, {})

        prev_times = existing.get("wallTimes", [])
        prev_times.append(round(wall_time, 1))
        avg_time = sum(prev_times) / len(prev_times)

        profile[key] = {
            "simTime":     round(sim_time, 6),
            "instructions": total_insts,
            "wallTime":    round(avg_time, 1),
            "wallTimes":   prev_times[-5:],
        }
        _save(profile)

    print(
        f"[profile] {key}: simTime={sim_time:.3f}s, "
        f"instructions={total_insts:,}, wallTime={wall_time:.0f}s (avg {avg_time:.0f}s)"
    )
