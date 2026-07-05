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

STRATEGY_DESC = {
    "Packed": "全スレッド → Node0 集中 (P→E順)",
    "Scatter": "ノード間インターリーブ (帯域分散)",
    "HPO": "P-core 優先: Node0 P → Node1 P → E-core",
    "MPO": "Jin本物の2段階DeLoc (utility.deloc_mapper.compute_deloc_map_from_csv で計算。"
           "ワークロード依存の静的推測ではなく、実測comm.csv/mem_access.csvに基づく)",
    "EPO": "E-core 優先: Node0 E → Node1 E → P-core (省電力重視、HPO の逆)",
    "SPO": "スケジューリング優先度協調制御 (暫定: Scatter 配置)",
    "RoundRobin": "ベースライン: CPU 0→15 順割り当て (NUMA・コア性能差無視)",
}


def get_cpu_map(strategy: str, workload: str) -> list:
    """
    ストラテジーとワークロードから cpu_map を返す。
    MPO は utility.deloc_mapper.compute_deloc_map_from_csv で計算するため、
    ここでは扱わない（呼び出し側は strategy=="MPO" を先に分岐すること。
    orchestrator.py/run.py/vsPOSM/vs_posm.py の _resolve_cpu_map を参照）。
    """
    if strategy not in STRATEGIES:
        print(f"[ERROR] 不明なストラテジー: {strategy}")
        sys.exit(1)
    return STRATEGIES[strategy]


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


