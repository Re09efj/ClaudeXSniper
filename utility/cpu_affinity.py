"""
cpu_affinity.py
Sniper シミュレーション用 cpu_map 定義と NPB バイナリ管理を担当する。

ストラテジー定義:
  Packed  : Node0 P-core に集中配置（NUMA ローカル最大化）
  Scatter : Node0/Node1 を交互インターリーブ（帯域分散）
  HPO     : P-core 優先。4TH まで Node0 P、5〜8TH で Node1 P、以降 E-core
  EPO     : E-core 優先。4TH まで Node0 E、5〜8TH で Node1 E、以降 P-core
  MPO     : メモリ親和性優先（ワークロード依存マッピング）
  RoundRobin: CPU 0 から順に割り当て（OS デフォルト相当のベースライン）
"""

import os
import subprocess
import sys

BINARY_BASE  = "/home/hiragahama/ClaudeXSniper/binary"
NPB_OMP_DIR  = f"{BINARY_BASE}/NPB3.3-OMP"
NPB_BIN_DIR  = os.path.join(NPB_OMP_DIR, "bin")
LAVAMD_DIR   = f"{BINARY_BASE}/Rodinia/openmp/lavaMD"
GAPBS_DIR    = f"{BINARY_BASE}/GAPBS"
FLUIDANIMATE_DIR = f"{BINARY_BASE}/PARSEC/pkgs/apps/fluidanimate/src"

GAPBS_WORKLOADS  = {"BFS", "PR", "BC", "CC", "SSSP", "TC"}
PARSEC_WORKLOADS = {"FLUIDANIMATE"}

# BENCH_CLASS → GAPBS グラフ頂点数スケール（2^g 頂点）
GAPBS_SCALE = {"S": 10, "W": 11, "A": 12, "B": 13, "C": 14, "D": 16}

# BENCH_CLASS → lavaMD boxes1d（総箱数 = boxes1d^3）
LAVAMD_BOXES = {"S": 3, "W": 4, "A": 5, "B": 8, "C": 10, "D": 15}


def binary_path(workload: str, bench_class: str) -> str:
    """ワークロード名とクラスからバイナリの絶対パスを返す。"""
    wl_upper = workload.upper()
    if workload.lower() == "lavamd":
        return f"{LAVAMD_DIR}/lavaMD"
    if wl_upper in GAPBS_WORKLOADS:
        return f"{GAPBS_DIR}/{workload.lower()}"
    if wl_upper in PARSEC_WORKLOADS:
        return f"{FLUIDANIMATE_DIR}/fluidanimate"
    return f"{NPB_BIN_DIR}/{workload.lower()}.{bench_class}.x"


def get_binary_args(workload: str, bench_class: str, num_threads: int) -> str:
    """ワークロード種別に応じた実行時引数を返す。"""
    wl_upper = workload.upper()
    if workload.lower() == "lavamd":
        boxes = LAVAMD_BOXES.get(bench_class, 3)
        return f"-cores {num_threads} -boxes1d {boxes}"
    if wl_upper in GAPBS_WORKLOADS:
        g = GAPBS_SCALE.get(bench_class, 10)
        args = {
            "BFS":  f"-g {g} -n 1",
            "PR":   f"-g {g} -i 10",
            "BC":   f"-g {g} -n 1",
            "CC":   f"-g {g}",
            "SSSP": f"-g {g} -n 1",
            "TC":   f"-g {g}",
        }
        return args.get(wl_upper, f"-g {g}")
    return ""

# NUMA レイアウト
# Node 0: CPU  0- 3 (P-core), CPU  4- 7 (E-core)
# Node 1: CPU  8-11 (P-core), CPU 12-15 (E-core)

STRATEGIES = {
    # ─── Packed ────────────────────────────────────────────────
    # Node0 P-core → Node0 E-core の順に詰め、Node0 満杯後は Node1 にオーバーフロー
    "Packed": [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,  # threads  0- 7: Node0 (P→E で詰める)
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,  # threads  8-15: Node0 満杯 → Node1 オーバーフロー
    ],
    # ─── Scatter ───────────────────────────────────────────────
    # Node0/Node1 を交互に割り当て、メモリ帯域を両ノードに分散
    "Scatter": [
        0,
        8,
        1,
        9,
        2,
        10,
        3,
        11,  # threads  0- 7: P-core インターリーブ
        4,
        12,
        5,
        13,
        6,
        14,
        7,
        15,  # threads  8-15: E-core インターリーブ
    ],
    # ─── HPO（Heuristic Priority Ordering）────────────────────
    # P-core を全優先。Node0 P → Node1 P → Node0 E → Node1 E の順
    # スレッド数が 4 以下なら全て Node0 P-core に収まる
    "HPO": [
        0,
        1,
        2,
        3,  # threads 0-3: Node0 P-core（4スレッドまでここで完結）
        8,
        9,
        10,
        11,  # threads 4-7: Node1 P-core（5スレッド目からこちら）
        4,
        5,
        6,
        7,  # threads 8-11: Node0 E-core
        12,
        13,
        14,
        15,  # threads12-15: Node1 E-core
    ],
    # ─── EPO（Efficiency Priority Ordering）───────────────────
    # E-core を全優先。HPO の逆順: Node0 E → Node1 E → Node0 P → Node1 P
    # 省電力重視。スレッド数が 4 以下なら全て Node0 E-core に収まる
    "EPO": [
        4,
        5,
        6,
        7,  # threads 0-3: Node0 E-core（4スレッドまでここで完結）
        12,
        13,
        14,
        15,  # threads 4-7: Node1 E-core
        0,
        1,
        2,
        3,  # threads 8-11: Node0 P-core
        8,
        9,
        10,
        11,  # threads12-15: Node1 P-core
    ],
    # ─── RoundRobin ────────────────────────────────────────────
    # OS デフォルトに相当するベースライン。CPU 0 から順に割り当て。
    # NUMA・コア性能差を一切考慮しない。
    "RoundRobin": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    # ─── SPO（Scheduling Priority Ordering）暫定 ──────────────
    # スケジューリング優先度の協調制御。cpu_map は後で設計予定。
    # 暫定として Scatter と同じ配置を使用する。
    "SPO": [
        0,
        8,
        1,
        9,
        2,
        10,
        3,
        11,  # threads  0- 7: Scatter と同じ（暫定）
        4,
        12,
        5,
        13,
        6,
        14,
        7,
        15,  # threads  8-15: 同上
    ],
}

# MPO はワークロード依存のため別定義
MPO_MAPS = {
    # CG: SpMV で全スレッドが共有密ベクトル z/p を読む。
    # first-touch によりこれらは Node0 に確保されるため Node0 先詰め。
    "CG": [
        0,
        1,
        2,
        3,  # threads 0-3: Node0 P-core（z/p がここにある）
        8,
        9,
        10,
        11,  # threads 4-7: Node1 P-core（帯域補強）
        4,
        5,
        6,
        7,  # threads 8-11: Node0 E-core
        12,
        13,
        14,
        15,  # threads12-15: Node1 E-core
    ],
    # BT: ブロック三重対角ソルバ。全スレッドが共有配列を高頻度でアクセスするため
    # first-touch で Node0 に確保された配列へのアクセスを最小化するよう Node0 先詰め。
    "BT": [
        0,
        1,
        2,
        3,  # threads 0-3: Node0 P-core
        8,
        9,
        10,
        11,  # threads 4-7: Node1 P-core
        4,
        5,
        6,
        7,  # threads 8-11: Node0 E-core
        12,
        13,
        14,
        15,  # threads12-15: Node1 E-core
    ],
    # MG: 3D格子をスラブ分割。隣接スラブを持つスレッドが境界で通信するため
    # 隣接スレッドペアを同一ノードに配置してノード内通信を最大化する。
    # (T0,T1)→Node0, (T2,T3)→Node1, (T4,T5)→Node0, ...
    "MG": [
        0,
        1,
        8,
        9,  # threads 0-3: T0,T1=Node0P / T2,T3=Node1P
        2,
        3,
        10,
        11,  # threads 4-7: T4,T5=Node0P / T6,T7=Node1P
        4,
        5,
        12,
        13,  # threads 8-11: E-core ペア
        6,
        7,
        14,
        15,  # threads12-15: E-core ペア
    ],
    # EP: Embarrassingly Parallel。スレッド間データ共有なし。
    # 各スレッドが独立した乱数列を処理するため first-touch で自ノードに確保される。
    # Node0 P-core 優先（HPO と同じ）でキャッシュ競合を最小化。
    "EP": [
        0, 1, 2, 3,          # threads 0-3: Node0 P-core
        8, 9, 10, 11,        # threads 4-7: Node1 P-core
        4, 5, 6, 7,          # threads 8-11: Node0 E-core
        12, 13, 14, 15,      # threads12-15: Node1 E-core
    ],
    # IS: Integer Sort。バケットソートで全スレッドが bucket_size 配列を共有読み書き。
    # first-touch で Node0 に確保されるため Node0 先詰めで遠隔アクセスを削減。
    "IS": [
        0, 1, 2, 3,          # threads 0-3: Node0 P-core
        8, 9, 10, 11,        # threads 4-7: Node1 P-core
        4, 5, 6, 7,          # threads 8-11: Node0 E-core
        12, 13, 14, 15,      # threads12-15: Node1 E-core
    ],
    # FT: FFT。グローバル転置が発生するため両ノードに帯域分散（Scatter と同じ）が有利。
    "FT": [
        0, 8, 1, 9, 2, 10, 3, 11,   # threads  0- 7: P-core インターリーブ
        4, 12, 5, 13, 6, 14, 7, 15,  # threads  8-15: E-core インターリーブ
    ],
    # SP: Scalar Pentadiagonal。BT と類似した三重対角ソルバ。
    # 全スレッドが共有配列を高頻度アクセス → Node0 先詰めで遠隔アクセスを削減。
    "SP": [
        0, 1, 2, 3,          # threads 0-3: Node0 P-core
        8, 9, 10, 11,        # threads 4-7: Node1 P-core
        4, 5, 6, 7,          # threads 8-11: Node0 E-core
        12, 13, 14, 15,      # threads12-15: Node1 E-core
    ],
    # GAPBS: グラフ処理は不規則メモリアクセス。両ノードの帯域を活用する Scatter 配置が有利。
    # BFS/BC/SSSP: フロンティアベースの探索 → Scatter で帯域分散
    # PR/CC/TC: 全ノード反復 → Scatter で帯域分散
    "BFS":  [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    "PR":   [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    "BC":   [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    "CC":   [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    "SSSP": [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    "TC":   [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15],
    # fluidanimate: 粒子シミュレーション。グリッド分割で隣接スレッドが境界を共有。
    # MG と同じ隣接ペア同ノード配置で境界通信をノード内に収める。
    "FLUIDANIMATE": [0, 1, 8, 9, 2, 3, 10, 11, 4, 5, 12, 13, 6, 7, 14, 15],
}

STRATEGY_DESC = {
    "Packed": "全スレッド → Node0 集中 (P→E順)",
    "Scatter": "ノード間インターリーブ (帯域分散)",
    "HPO": "P-core 優先: Node0 P → Node1 P → E-core",
    "MPO": "メモリ親和性優先 (ワークロード依存: CG/BT/IS/SP=Node0先詰め / MG=隣接ペア同ノード / EP=HPO / FT=Scatter / lavaMD=Scatter)",
    "EPO": "E-core 優先: Node0 E → Node1 E → P-core (省電力重視、HPO の逆)",
    "SPO": "スケジューリング優先度協調制御 (暫定: Scatter 配置)",
    "RoundRobin": "ベースライン: CPU 0→15 順割り当て (NUMA・コア性能差無視)",
}


def get_cpu_map(strategy: str, workload: str) -> list:
    """ストラテジーとワークロードから cpu_map を返す。MPO のみワークロード依存。"""
    if strategy == "MPO":
        key = workload.upper()
        # lavaMD は不規則アクセス → Scatter と同じ配置
        if key == "LAVAMD":
            return STRATEGIES["Scatter"]
        if key not in MPO_MAPS:
            print(f"[WARNING] MPO map for '{workload}' 未定義。Scatter 配置を使用します。")
            return STRATEGIES["Scatter"]
        return MPO_MAPS[key]
    if strategy not in STRATEGIES:
        print(f"[ERROR] 不明なストラテジー: {strategy}")
        sys.exit(1)
    return STRATEGIES[strategy]


MPO_EQUIVALENT_CANDIDATES = ("Packed", "Scatter", "HPO", "EPO")


def resolve_mpo_equivalent(workload: str, num_threads: int) -> str | None:
    """
    指定ワークロード・スレッド数で MPO の cpu_map (先頭 num_threads 件) が
    他の主要戦略と完全一致するかを判定する。

    一致すれば一致先の戦略名を返す（実シミュレーション不要、結果を複製流用できる）。
    一致しなければ None を返す（MPO 専用の実シミュレーションが必要）。
    """
    mpo_map = get_cpu_map("MPO", workload)[:num_threads]
    for name in MPO_EQUIVALENT_CANDIDATES:
        if STRATEGIES[name][:num_threads] == mpo_map:
            return name
    return None



def save_affinity_config(
    output_dir: str,
    preset_name: str,
    workload: str,
    bench_class: str,
    cpu_map: list,
    num_threads: int,
    num_nodes: int = 2,
    big_cpn: int = 4,
    sml_cpn: int = 4,
) -> None:
    """実験条件を affinity_config.txt として output_dir に保存する。"""
    import os
    path = os.path.join(output_dir, "affinity_config.txt")
    lines = [
        f"BENCHMARK={workload.upper()}.{bench_class} (NPB3.3 OpenMP)",
        f"PRESET={preset_name}",
        f"NUM_THREADS={num_threads}",
        f"NUM_NODES={num_nodes}",
        f"BIG_CORES_PER_NODE={big_cpn}",
        f"SMALL_CORES_PER_NODE={sml_cpn}",
        f"cpu_map={cpu_map}",
        "",
        "# Thread -> CPU -> Node mapping",
    ]
    for t in range(min(num_threads, 16)):
        cpu  = cpu_map[t]
        node = 0 if cpu < 8 else 1
        ctype = "P" if (cpu % 8) < 4 else "E"
        lines.append(f"  thread{t:02d} -> CPU{cpu:02d} (Node{node} {ctype}-core)")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[config] 保存: {path}")


def recompile_workload(workload: str, bench_class: str = "S") -> None:
    """ワークロードのバイナリを再コンパイルする。"""
    wl_upper = workload.upper()

    if workload.lower() == "lavamd":
        print(f"[compile] make lavaMD ...")
        result = subprocess.run(["make"], cwd=LAVAMD_DIR, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[ERROR] コンパイル失敗 (lavaMD)\n{result.stderr[-2000:]}")
            sys.exit(1)
        print(f"[compile] lavaMD 完了")
        return

    if wl_upper in GAPBS_WORKLOADS:
        print(f"[compile] GAPBS {workload} (make) ...")
        result = subprocess.run(
            ["make", workload.lower()], cwd=GAPBS_DIR, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[ERROR] コンパイル失敗 (GAPBS/{workload})\n{result.stderr[-2000:]}")
            sys.exit(1)
        print(f"[compile] GAPBS {workload} 完了")
        return

    if wl_upper in PARSEC_WORKLOADS:
        if wl_upper == "FLUIDANIMATE":
            print(f"[compile] fluidanimate (make -f Makefile.pthreads) ...")
            result = subprocess.run(
                ["make", "-f", "Makefile.pthreads"],
                cwd=FLUIDANIMATE_DIR, capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"[ERROR] コンパイル失敗 (fluidanimate)\n{result.stderr[-2000:]}")
                sys.exit(1)
            print(f"[compile] fluidanimate 完了")
            return

    # NPB
    target = workload.lower()
    print(f"[compile] make {target} CLASS={bench_class} ...")
    result = subprocess.run(
        ["make", target, f"CLASS={bench_class}"],
        cwd=NPB_OMP_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] コンパイル失敗 ({workload})\n{result.stderr[-2000:]}")
        sys.exit(1)
    print(f"[compile] {workload} Class {bench_class} 完了")


