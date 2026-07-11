"""
generate_config.py
一般的な P/E ヘテロジニアス マルチコア (2 NUMA ノード x (Big core + Small core)) を
想定した Sniper 設定ファイルを、戦略・スレッド数ごとに動的生成する。

2026-07-06: 実機の公表値に基づく合成モデルに変更。P-coreは筆者の手元にある
実機 Intel Core i7-1195G7 (Tiger Lake, Willow Cove アーキテクチャ) の実測値、
E-coreは同時代に実在するIntelの小コア Gracemont (Alder Lakeで初出荷) の
公表値を援用している。2ソケット/NUMA構成自体は現実に存在する製品ではなく、
本研究のための架空の組み合わせである点は変わらない。

トポロジ (物理):
  CPU  0- 3 : P-core (Big),   2.9 GHz, Node0
  CPU  4- 7 : E-core (Small), 2.2 GHz, Node0
  CPU  8-11 : P-core (Big),   2.9 GHz, Node1
  CPU 12-15 : E-core (Small), 2.2 GHz, Node1
  (P:E周波数比 ~1.3倍は Intel ハイブリッド系列で広く観測される比率と一致
   例: Core i9-12900K ベースクロック 3.2GHz(P)/2.4GHz(E) = 1.33倍)

NUMAレイテンシ設定:
  ローカルDRAM   : 60 ns (perf_model/dram/latency, DDR4-3200/LPDDR4x-4266の
                   実測レイテンシ域として妥当)
  リモートペナルティ: 帯域幅競合による自然な遅延 (bus モデル)
    → emesh_hop_counter は使用しない
    → P/E コアで hop_latency がサイクル数で異なる問題を回避
    → Jin (2022) と同じアプローチ

キャッシュサイズ (実機公表値ベース):
  L1-D  : P-core 48KB (Willow Cove実測) / E-core 32KB (Gracemont実測)
  L2    : P-core 1.25MB (Willow Cove実測) / E-core 512KB
          (Gracemont実測: 4コアクラスタ共有2MBを4分割した按分値。本モデルは
           クラスタ共有L2を実装せずper-core privateとして簡略化している)
  L3    : 12MB (Tiger Lake 4コアSKUの実測値をノードあたりの値として採用、
          2ノード合計24MB)

コア幅・ROB (実機公表値ベース、Chips and Cheese等のマイクロアーキ解析より):
  dispatch_width : P-core 5 (Willow Cove) / E-core 3 (Gracemontの
                   クラスタ型デコーダを踏まえ保守的な値)
  ROB(window_size): P-core 352 (Willow Cove) / E-core 256 (Gracemont)

technology_node = 10 (10nm) は Tiger Lake の実際の製造プロセス(Intel 10nm
SuperFin)と一致する。

per_controller_bandwidth = 51.2 GB/s は DDR4-3200 デュアルチャネル構成の
実測帯域と一致(i7-1195G7の実メモリ構成に対応)。

Sniper のコアは cpu_map の順に割り当てられる:
  simulated core i → ordered_cpus[i] の特性 (周波数・NUMA ノード)
"""

import os

P_CORES    = set(range(0, 4))  | set(range(8, 12))
E_CORES    = set(range(4, 8))  | set(range(12, 16))
NODE0_CPUs = set(range(0, 8))
NODE1_CPUs = set(range(8, 16))

P_FREQ = 2.9   # GHz (i7-1195G7 実測ベースクロック)
E_FREQ = 2.2   # GHz (P:E比 ~1.3倍を維持したGracemont相当値)

LOCAL_LATENCY_NS = 60   # ローカルDRAMアクセスレイテンシ

# 2026-07-11: AKARIN新数式(akarin/cpsat_mapper.py、roofline型objective)がこの
# 帯域値を最適化計算に直接使うため、以前のP_FREQ/E_FREQと同じ理由(2026-07-09に
# generate_config.py側の値変更にakarin側が追従できず不整合が起きた事例)で、
# ハードコード値ではなく単一の真実源として名前付き定数化した。
PER_CONTROLLER_BANDWIDTH_GBPS = 51.2  # [perf_model/dram] per_controller_bandwidth
BUS_BANDWIDTH_GBPS = 102.4            # [network/bus] bandwidth

# キャッシュサイズ (KB) - 実機公表値ベース (Willow Cove / Gracemont)
L1D_KB_P = 48
L1D_KB_E = 32
L2_KB_P  = 1280   # 1.25MB (Willow Cove実測)
L2_KB_E  = 512    # Gracemont 4コアクラスタ共有2MBの按分値
L3_KB    = 12288  # 12MB、ノードあたり (Tiger Lake 4コアSKU実測)

# コア幅・ROB (KB表記に合わせROB_*/DISPATCH_*として定義。実機公表値ベース)
ROB_P            = 352   # Willow Cove実測
ROB_E            = 256   # Gracemont実測
DISPATCH_WIDTH_P = 5     # Willow Cove
DISPATCH_WIDTH_E = 3     # Gracemont (クラスタ型デコーダのため保守的な値)


def _core_freq(cpu_id: int) -> float:
    return P_FREQ if cpu_id in P_CORES else E_FREQ


def _core_node(cpu_id: int) -> int:
    return 0 if cpu_id in NODE0_CPUs else 1


TOTAL_SIM_CORES = 16  # シミュレート対象トポロジは常にフル16固定 (Jin方式、2026-07-10)

def _active_dram_controllers(cpu_map: list, num_threads: int) -> tuple[int, list[int]]:
    """
    このジョブでアクティブなスレッド集合(cpu_map[:num_threads])がNode0/Node1の
    どちらか一方に偏っているか、両方にまたがっているかに応じて、DRAMコントローラの
    個数と配置位置を返す。2026-07-06に実測検証済みの「帯域競合(コントローラを
    何個使えるか)」効果をtotal_cores固定化後も維持するため、Sniperの自動interleaving
    計算(total_coresに対する割合で決まる)ではなく、num_controllers/controller_positions
    を明示指定する方式にする(2026-07-10、Jin方式へのtotal_cores固定化に伴う変更。
    詳細はDocuments/2026年7月10日.mdの「範囲外バグ」節を参照)。
    """
    active = cpu_map[:num_threads]
    n0 = sum(1 for c in active if c in NODE0_CPUs)
    n1 = num_threads - n0
    if n0 == 0:
        return 1, [8]   # Node1のみ使用 → コントローラ1個(Node1側)
    if n1 == 0:
        return 1, [0]   # Node0のみ使用 → コントローラ1個(Node0側)
    return 2, [0, 8]    # 両ノードにまたがる → コントローラ2個


def _write_map_file(cpu_map: list, num_threads: int, map_path: str) -> None:
    """
    Jin方式のmap_file(scheduler/pinned_map用)を書き出す。`thread_id:cpu_id`
    形式(/home/agung/vcs/sniper-configs/sniper-mapping/*.mapと同じ形式)。
    """
    with open(map_path, "w") as f:
        for t in range(num_threads):
            f.write(f"{t}:{cpu_map[t]}\n")


def get_map_path(output_dir: str, strategy: str, num_threads: int) -> str:
    """map_fileの保存パスを返す(ファイルは生成しない)。get_config_pathと対になる。"""
    return os.path.join(output_dir, f"arrow_lake_{strategy}_{num_threads}TH.map")


def generate_config(
    strategy: str,
    num_threads: int,
    cpu_map: list,
    output_path: str,
    map_file_container_path: str,
) -> str:
    """
    P/Eヘテロジニアス設定ファイルを生成して output_path に書き込み、パスを返す。

    2026-07-10: GOMP_CPU_AFFINITY(実行時syscall経由の配置)がこの環境では機能
    しないと判明したため、JinのSniperフォークが持つscheduler/pinned_map
    (thread_id:cpu_idを静的configファイルで指定、SIDのSniperイメージにも
    同じパッチを移植・再ビルド済み)に統一した。map_file_container_pathには、
    実行環境(SIDコンテナ内 or Purpleリモート)から見た.mapファイルの絶対パスを
    呼び出し側が渡すこと。実体の.mapファイルはoutput_pathと同じディレクトリに
    書き出す(get_map_path参照)ので、Purple向けの場合は呼び出し側が転送を担当。
    """
    # total_cores は常にフル16固定 (Jin方式、2026-07-10)。cpu_mapは物理CPU番号
    # 0〜15の固定リストなので、simulated core index == physical CPU番号となり
    # 常に有効な範囲に収まる(以前はnum_threadsに縮小していたため、Scatter/EPO/
    # HPO/MPOがnum_threads未満の実行でcpu_mapの値がapplicationCoresを超え、
    # GOMP_CPU_AFFINITY経由の配置が範囲外になりうるバグがあった)。
    # 使われない残りのシミュレートコアは単にアイドルのまま。
    ordered_cpus = list(range(TOTAL_SIM_CORES))
    n_cores      = TOTAL_SIM_CORES
    num_controllers, controller_positions = _active_dram_controllers(cpu_map, num_threads)
    controller_positions_str = ",".join(str(p) for p in controller_positions)

    map_path = os.path.splitext(output_path)[0] + ".map"
    _write_map_file(cpu_map, num_threads, map_path)

    # shared_cores: L3はノードごとの物理構成(8コア/ノード)に固定。DRAMコントローラの
    # 個数(num_controllers、帯域競合効果を出すためアクティブなノード数で可変)とは
    # 別の、トポロジ自体の固定プロパティなので混同しない。
    shared_cores = 8

    # per-core 周波数リスト (GHz)
    freqs = [_core_freq(c) for c in ordered_cpus]
    freq_str = ",".join(f"{f:.1f}" for f in freqs)

    # dispatch_width / window_size (P/E コア別、Willow Cove/Gracemont実機公表値ベース)
    dispatch = [DISPATCH_WIDTH_P if c in P_CORES else DISPATCH_WIDTH_E for c in ordered_cpus]
    rob_size  = [ROB_P if c in P_CORES else ROB_E for c in ordered_cpus]
    dispatch_str = ",".join(str(d) for d in dispatch)
    rob_str      = ",".join(str(r) for r in rob_size)

    # L1-D (KB)
    l1d_size = [L1D_KB_P if c in P_CORES else L1D_KB_E for c in ordered_cpus]
    l1d_str  = ",".join(str(s) for s in l1d_size)

    # L2 (KB)
    l2_size = [L2_KB_P if c in P_CORES else L2_KB_E for c in ordered_cpus]
    l2_str  = ",".join(str(s) for s in l2_size)

    cfg = f"""\
# Heterogeneous P/E core system - strategy={strategy}, threads={num_threads}
# Generated by ClaudeXSniper/config/generate_config.py
# Ordered CPUs: {ordered_cpus}
#
# NUMA penalty: bus model (bandwidth-contention-based, no fixed hop latency)
# Cache sizes : L1-D={L1D_KB_P}KB  L2(P)={L2_KB_P}KB L2(E)={L2_KB_E}KB  L3={L3_KB}KB

#include nehalem
#include gainestown

[general]
total_cores = {n_cores}
enable_icache_modeling = true
# 2026-07-10: First-Touch(AddressHomeLookup)とバス課金のノード単位免除
# (NetworkModelBus)の両方が参照するノード境界。Node0=CPU0-7, Node1=CPU8-15
# に固定(トポロジ自体の物理プロパティ、戦略に依らず不変)。
cores_per_node = 8

[perf_model/core]
type = interval
frequency = {P_FREQ:.1f}
frequency[] = {freq_str}

[perf_model/core/interval_timer]
dispatch_width = {DISPATCH_WIDTH_P}
dispatch_width[] = {dispatch_str}
window_size = {ROB_P}
window_size[] = {rob_str}

[perf_model/l1_dcache]
cache_size = {L1D_KB_P}
cache_size[] = {l1d_str}
address_hash = mod

[perf_model/l2_cache]
cache_size = {L2_KB_P}
cache_size[] = {l2_str}
associativity = 8
address_hash = mod

[perf_model/l3_cache]
cache_size = {L3_KB}
associativity = 16
shared_cores = {shared_cores}
address_hash = mod

[perf_model/dram]
num_controllers = {num_controllers}
controller_positions = {controller_positions_str}
latency = {LOCAL_LATENCY_NS}
per_controller_bandwidth = {PER_CONTROLLER_BANDWIDTH_GBPS}

[scheduler]
type = pinned_map

[scheduler/pinned]
# GOMP_CPU_AFFINITY(実行時syscall経由)はこの環境で機能しないと2026-07-10に判明。
# Jinのpinned_mapパッチ(SID/Purple双方のSniperビルドに移植済み)でmap_file
# (thread_id:cpu_id、_write_map_file()参照)による厳密な静的配置を使う。
map_file = {map_file_container_path}

[network]
memory_model_1 = bus
memory_model_2 = bus

[network/bus]
bandwidth = {BUS_BANDWIDTH_GBPS}
ignore_local_traffic = true

[dvfs/simple]
cores_per_socket = {n_cores}

[power]
vdd = 1.0
technology_node = 10
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(cfg)

    return output_path


def get_config_path(
    output_dir: str, strategy: str, num_threads: int
) -> str:
    """設定ファイルの保存パスを返す（ファイルは生成しない）。"""
    return os.path.join(output_dir, f"arrow_lake_{strategy}_{num_threads}TH.cfg")
