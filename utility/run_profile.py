"""
run_profile.py
実行プロファイル（シミュレーション時間・命令数）を JSON に保存・取得する。
Sniper 版: sim.stats.sqlite3 / sim.info から読む。
"""

import json
import os
import statistics
import tempfile
import threading

_lock = threading.Lock()

PROFILE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "Data", "run_profile.json"
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


# クラスの順序（小→大）
_CLASS_ORDER = ['S', 'W', 'A', 'B', 'C', 'D']

# 経験データがない場合の per-step デフォルト倍率（タイムアウト安全のため実測値より若干大きめ）
# S→W 実測中央値をベースに設定（W→A 以降にも同じ倍率を適用）
_DEFAULT_STEP: dict[str, float] = {
    'BFS': 3.0,  'BC': 3.0,  'PR': 3.0,
    'CC':  3.0,  'SSSP': 3.0, 'TC': 3.0,   # GAPBS: 実測 ~2.3×
    'lavaMD': 2.0,                           # Rodinia: 実測 ~1.0×
    'FT':  5.0,                              # 実測 3.1×
    'CG':  15.0,                             # 実測 13.2×
    'IS':  25.0,                             # 実測 21.5×
    'MG':  90.0,                             # 実測 82.2×
    'BT':  40.0,                             # 実測 33.7×
    'SP':  180.0,                            # 実測 175.8×
}
_DEFAULT_STEP_FALLBACK = 30.0
_MAX_ESTIMATED_SEC     = 14400.0  # 推定値の上限 4 時間 → timeout max = 12 時間


def get_reference(workload: str, bench_class: str, num_threads: int) -> dict | None:
    """{"simTime": float, "instructions": int, "wallTime": float} または None。"""
    with _lock:
        return _load().get(_key(workload, bench_class, num_threads))


def _empirical_step_ratio(workload: str, lo_cls: str, hi_cls: str,
                           profile: dict) -> float:
    """lo_cls → hi_cls の実測スケール比（中央値）。データなければデフォルト倍率。"""
    ratios = []
    for th in [2, 4, 8, 16]:
        lo_key = _key(workload, lo_cls, th)
        hi_key = _key(workload, hi_cls, th)
        if lo_key in profile and hi_key in profile:
            ratios.append(profile[hi_key]['wallTime'] / profile[lo_key]['wallTime'])
    if ratios:
        return statistics.median(ratios)
    return _DEFAULT_STEP.get(workload, _DEFAULT_STEP_FALLBACK)


def _class_scale(workload: str, from_cls: str, to_cls: str, profile: dict) -> float:
    """from_cls → to_cls の推定倍率（複数ステップは乗算）。"""
    fi = _CLASS_ORDER.index(from_cls)
    ti = _CLASS_ORDER.index(to_cls)
    scale = 1.0
    for i in range(fi, ti):
        scale *= _empirical_step_ratio(
            workload, _CLASS_ORDER[i], _CLASS_ORDER[i + 1], profile
        )
    return scale


def estimate_walltime(workload: str, bench_class: str, num_threads: int) -> float | None:
    """
    プロファイルにエントリがない場合の wallTime 推定（タイムアウト設定用）。

    優先順位:
      1. 完全一致（exact match）
      2. 同 wl+th、小さいクラスから等比外挿
      3. 同 wl+class、別スレッド数からスレッドスケーリング
      4. 同 wl、別クラス×別スレッド数の組み合わせ
    上限: _MAX_ESTIMATED_SEC (4h)
    """
    with _lock:
        profile = _load()

    exact = profile.get(_key(workload, bench_class, num_threads))
    if exact:
        return exact['wallTime']

    try:
        target_idx = _CLASS_ORDER.index(bench_class)
    except ValueError:
        return None

    # 戦略 1: 同 wl+th、より小さいクラスから外挿
    for cls in _CLASS_ORDER[target_idx - 1::-1]:
        ref_key = _key(workload, cls, num_threads)
        if ref_key in profile:
            scale = _class_scale(workload, cls, bench_class, profile)
            est   = profile[ref_key]['wallTime'] * scale
            print(f"[profile/estimate] {_key(workload, bench_class, num_threads)}: "
                  f"{cls}×{scale:.1f} → {est:.0f}s (ref={profile[ref_key]['wallTime']:.0f}s)")
            return min(est, _MAX_ESTIMATED_SEC)

    # 戦略 2: 同 wl+class、別スレッド数（スレッドスケールは楽観的に線形仮定）
    for th in sorted([2, 4, 8, 16], key=lambda t: abs(t - num_threads)):
        if th == num_threads:
            continue
        ref_key = _key(workload, bench_class, th)
        if ref_key in profile:
            th_scale = max(th / num_threads, 0.5)
            est = profile[ref_key]['wallTime'] * th_scale
            print(f"[profile/estimate] {_key(workload, bench_class, num_threads)}: "
                  f"{bench_class}/{th}TH×{th_scale:.2f} → {est:.0f}s")
            return min(est, _MAX_ESTIMATED_SEC)

    # 戦略 3: 同 wl、別クラス+別スレッド数の組み合わせ
    for cls in _CLASS_ORDER[target_idx - 1::-1]:
        for th in sorted([2, 4, 8, 16], key=lambda t: abs(t - num_threads)):
            ref_key = _key(workload, cls, th)
            if ref_key in profile:
                class_scale = _class_scale(workload, cls, bench_class, profile)
                th_scale    = max(th / num_threads, 0.5)
                est = profile[ref_key]['wallTime'] * class_scale * th_scale
                print(f"[profile/estimate] {_key(workload, bench_class, num_threads)}: "
                      f"{cls}/{th}TH×{class_scale:.1f}×{th_scale:.2f} → {est:.0f}s")
                return min(est, _MAX_ESTIMATED_SEC)

    return None


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
