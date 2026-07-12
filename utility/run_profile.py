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


def _key(workload: str, bench_class: str, num_threads: int, machine: str = "sid") -> str:
    """
    プロファイルキー。machine="sid"(既定)は従来通り接尾辞なし(後方互換)。
    machine="purple"など他マシンは"@machine"を付与し、実行環境が異なるため
    wallTime統計を混同しないよう分離する(sidとpurpleでは実測walltimeが
    系統的に2〜3倍異なることが判明したため、2026-07-05導入)。
    """
    base = f"{workload}_{bench_class}_{num_threads}"
    return base if machine == "sid" else f"{base}@{machine}"


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
    # canneal/dedup/GUPSは2026-07-06〜08にget_binary_args()がCANNEAL_PARAMS/
    # DEDUP_INPUT_BY_CLASS/GUPS_LOG2_SIZEでbench_classに応じてスケールする
    # よう修正済み(cpu_affinity.py参照)。以前はここに「入力が完全に同一だから
    # 1.0」という前提を書いていたが、2026-07-11のSizeW準備中の実測で既に
    # 前提が崩れていたと判明(このコメントも含め古いまま放置されていた)。
    # 実測S→W比率(複数スレッド数の中央値、Data/run_profile.json参照):
    #   canneal ≈1.41(Wの方が重い、nswaps 10000→40000と直感通り)
    #   dedup   ≈0.73(意外にもWの方が速い。media_w.datは2連結の高冗長ファイル
    #            のため、重複検出が効きやすく処理が速く済むと推測。物理的に
    #            妥当な説明はあるが要注意な逆転現象)
    #   GUPS    ≈2.4 (log2_size 22→23でテーブルサイズ2倍、直感通り)
    # ただしこれらの値はestimate_walltime()の戦略0〜3で実測ペアが1つでも
    # 見つかればそちらが優先され(2026-07-11にスレッド候補を固定リストから
    # 動的探索に変更済み)、ここは「そのワークロード×クラスの実測が本当に
    # 一切無い」場合の最終フォールバックとしてのみ使われる。
    # x264は恒久除外ワークロード(2026-07-07判明の設計非互換)のため実測なし、
    # 汎用フォールバックとの中間的な値として1.0のまま残す。
    'canneal': 1.41, 'dedup': 0.73, 'x264': 1.0, 'GUPS': 2.4,
}
_DEFAULT_STEP_FALLBACK = 30.0
_MAX_ESTIMATED_SEC     = 14400.0  # 推定値の上限 4 時間 → timeout max = 12 時間


def get_reference(workload: str, bench_class: str, num_threads: int, machine: str = "sid") -> dict | None:
    """{"simTime": float, "instructions": int, "wallTime": float} または None。"""
    with _lock:
        return _load().get(_key(workload, bench_class, num_threads, machine))


def _empirical_step_ratio(workload: str, lo_cls: str, hi_cls: str,
                           profile: dict, machine: str = "sid") -> float:
    """
    lo_cls → hi_cls の実測スケール比（中央値）。データなければデフォルト倍率。

    2026-07-11: 候補スレッド数を固定リスト[2,4,8,16]からprofile実データ由来の
    動的リスト(_threads_seen_for)に変更。dedup(実スレッド数6/9/12/15)や
    GUPS(SIDでの実測が12のみ等)のように固定リストと噛み合わないワークロードで、
    実測ペアがあるのに見つからずstaleな_DEFAULT_STEPへ落ちる不具合があった
    (SizeWバッチ準備中に発覚。dedup_S_6@purpleのタイムアウト事故と同型)。
    """
    ratios = []
    for th in _threads_seen_for(workload, profile):
        lo_key = _key(workload, lo_cls, th, machine)
        hi_key = _key(workload, hi_cls, th, machine)
        if lo_key in profile and hi_key in profile:
            ratios.append(profile[hi_key]['wallTime'] / profile[lo_key]['wallTime'])
    if ratios:
        return statistics.median(ratios)
    return _DEFAULT_STEP.get(workload, _DEFAULT_STEP_FALLBACK)


def _threads_seen_for(workload: str, profile: dict) -> set[int]:
    """
    profile内のキーを実際にスキャンして、このワークロードでこれまでに
    記録された全スレッド数(クラス・マシン問わず)を集合で返す。

    2026-07-11: 以前は候補スレッド数を[2,4,8,16]のように固定リストで
    決め打ちしていたため、dedup(実スレッド数6/9/12/15)やGUPS(SIDでは
    2/8/12はあるが16は無い等)のように標準リストとズレるワークロードで、
    実測ペアが存在するのに固定リストに含まれず見つからない、という
    バグが繰り返し起きた(dedup_S_6@purpleのタイムアウト事故が最初の発覚例)。
    固定リストをやめ、profileの実データから動的に候補を拾うことで、
    どんなスレッド数の癖を持つワークロードでも自動的に対応できるようにした。
    """
    threads = set()
    prefix = f"{workload}_"
    for key in profile:
        # key形式: "{workload}_{class}_{threads}" または "...@{machine}"
        base = key.split("@", 1)[0]
        if not base.startswith(prefix):
            continue
        rest = base[len(prefix):]
        parts = rest.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            threads.add(int(parts[1]))
    return threads


def _empirical_machine_ratio(workload: str, bench_class: str, profile: dict, machine: str) -> float | None:
    """
    同ワークロード・同クラスで、SIDと対象マシン(machine)の両方に実測がある
    スレッド数から、machine/sidのwallTime比率(中央値)を計算する。見つから
    なければNone。

    2026-07-11追加: dedup_S_6/S_9@purpleのように、対象マシンでの実測が
    全く無いワークロード×スレッド数の見積もりに使う。同一マシン内の素朴な
    スレッド数線形外挿(_empirical_step_ratio等)は、dedupのような非単調な
    ワークロードでは大外れする(実際にタイムアウト事故を起こした)。
    ワークロード自体のスレッド数依存の癖はマシンが変わっても概ね同じはず、
    という前提で、「マシン間の系統的な速度比」を別スレッド数から借りてくる
    方が、素朴な線形スケーリングより物理的に妥当と判断した。
    """
    ratios = []
    for th in _threads_seen_for(workload, profile):
        sid_key = _key(workload, bench_class, th, "sid")
        m_key   = _key(workload, bench_class, th, machine)
        if sid_key in profile and m_key in profile:
            ratios.append(profile[m_key]['wallTime'] / profile[sid_key]['wallTime'])
    if ratios:
        return statistics.median(ratios)
    return None


def _class_scale(workload: str, from_cls: str, to_cls: str, profile: dict, machine: str = "sid") -> float:
    """from_cls → to_cls の推定倍率（複数ステップは乗算）。"""
    fi = _CLASS_ORDER.index(from_cls)
    ti = _CLASS_ORDER.index(to_cls)
    scale = 1.0
    for i in range(fi, ti):
        scale *= _empirical_step_ratio(
            workload, _CLASS_ORDER[i], _CLASS_ORDER[i + 1], profile, machine
        )
    return scale


def estimate_walltime(workload: str, bench_class: str, num_threads: int, machine: str = "sid") -> float | None:
    """
    プロファイルにエントリがない場合の wallTime 推定(タイムアウト設定用)。
    machineごとに参照キーが分離されているため、sid/purpleを混同しない。

    優先順位:
      1. 完全一致（exact match）
      2. 同 wl+th、小さいクラスから等比外挿
      3. 同 wl+class、別スレッド数からスレッドスケーリング
      4. 同 wl、別クラス×別スレッド数の組み合わせ
    上限: _MAX_ESTIMATED_SEC (4h)
    """
    with _lock:
        profile = _load()

    exact = profile.get(_key(workload, bench_class, num_threads, machine))
    if exact:
        return exact['wallTime']

    # 戦略 0: (SID以外のマシン限定) 同ワーク+同クラスの他スレッド数で実測
    # されたmachine/SID比率を、対象スレッド数のSID実測に適用する。同一
    # マシン内の素朴なスレッド数線形外挿より優先する(_empirical_machine_ratio
    # のdocstring参照)。
    if machine != "sid":
        sid_exact_key = _key(workload, bench_class, num_threads, "sid")
        if sid_exact_key in profile:
            ratio = _empirical_machine_ratio(workload, bench_class, profile, machine)
            if ratio is not None:
                est = profile[sid_exact_key]['wallTime'] * ratio
                # print(f"[profile/estimate] {_key(workload, bench_class, num_threads, machine)}: "
                #       f"sid実測×machine比率{ratio:.2f} → {est:.0f}s "
                #       f"(sid_ref={profile[sid_exact_key]['wallTime']:.0f}s)")
                return min(est, _MAX_ESTIMATED_SEC)

    try:
        target_idx = _CLASS_ORDER.index(bench_class)
    except ValueError:
        return None

    # 戦略 1: 同 wl+th、より小さいクラスから外挿
    for cls in _CLASS_ORDER[target_idx - 1::-1]:
        ref_key = _key(workload, cls, num_threads, machine)
        if ref_key in profile:
            scale = _class_scale(workload, cls, bench_class, profile, machine)
            est   = profile[ref_key]['wallTime'] * scale
            # print(f"[profile/estimate] {_key(workload, bench_class, num_threads, machine)}: "
            #       f"{cls}×{scale:.1f} → {est:.0f}s (ref={profile[ref_key]['wallTime']:.0f}s)")
            return min(est, _MAX_ESTIMATED_SEC)

    # 戦略 2: 同 wl+class、別スレッド数（スレッドスケールは楽観的に線形仮定）
    # 2026-07-11: 候補を固定リスト[2,4,8,16]から_threads_seen_for(動的)に変更
    # (_empirical_step_ratioと同じ理由、dedup/GUPSで固定リストとの不一致が発覚)。
    for th in sorted(_threads_seen_for(workload, profile), key=lambda t: abs(t - num_threads)):
        if th == num_threads:
            continue
        ref_key = _key(workload, bench_class, th, machine)
        if ref_key in profile:
            th_scale = max(th / num_threads, 0.5)
            est = profile[ref_key]['wallTime'] * th_scale
            # print(f"[profile/estimate] {_key(workload, bench_class, num_threads, machine)}: "
            #       f"{bench_class}/{th}TH×{th_scale:.2f} → {est:.0f}s")
            return min(est, _MAX_ESTIMATED_SEC)

    # 戦略 3: 同 wl、別クラス+別スレッド数の組み合わせ
    for cls in _CLASS_ORDER[target_idx - 1::-1]:
        for th in sorted(_threads_seen_for(workload, profile), key=lambda t: abs(t - num_threads)):
            ref_key = _key(workload, cls, th, machine)
            if ref_key in profile:
                class_scale = _class_scale(workload, cls, bench_class, profile, machine)
                th_scale    = max(th / num_threads, 0.5)
                est = profile[ref_key]['wallTime'] * class_scale * th_scale
                # print(f"[profile/estimate] {_key(workload, bench_class, num_threads, machine)}: "
                #       f"{cls}/{th}TH×{class_scale:.1f}×{th_scale:.2f} → {est:.0f}s")
                return min(est, _MAX_ESTIMATED_SEC)

    return None


def update_from_run(
    workload: str,
    bench_class: str,
    num_threads: int,
    output_dir: str,
    wall_time: float,
    machine: str = "sid",
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
        key = _key(workload, bench_class, num_threads, machine)
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
