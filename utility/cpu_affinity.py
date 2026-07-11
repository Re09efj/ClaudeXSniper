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
CANNEAL_DIR  = f"{BINARY_BASE}/PARSEC/pkgs/kernels/canneal/src"
DEDUP_DIR    = f"{BINARY_BASE}/PARSEC/pkgs/kernels/dedup/src"
X264_DIR     = f"{BINARY_BASE}/PARSEC/pkgs/apps/x264/src"
BODYTRACK_DIR = f"{BINARY_BASE}/PARSEC/pkgs/apps/bodytrack/src"
WATER_NSQUARED_DIR = f"{BINARY_BASE}/PARSEC/ext/splash2/apps/water_nsquared/src"
RADIOSITY_DIR       = f"{BINARY_BASE}/PARSEC/ext/splash2/apps/radiosity/src"
GUPS_DIR     = f"{BINARY_BASE}/GUPS"

GAPBS_WORKLOADS  = {"BFS", "PR", "BC", "CC", "SSSP", "TC"}
# GUPS(HPCC RandomAccess): テーブルサイズがシミュレート対象L3(4MB、config/
# generate_config.pyのL3_KB参照)を大きく超えるよう設計された、計算をほぼ
# 持たない純粋ランダムアクセスカーネル。canneal(ランダムだがnetlist上の
# 実コスト計算を伴う)やGAPBS(グラフ構造上のポインタチェイス)とも異なり、
# NUMA相互接続そのものの効果を最も素直に測れる基準点として2026-07-06に追加。
GUPS_WORKLOADS = {"GUPS"}
PARSEC_WORKLOADS = {"FLUIDANIMATE", "CANNEAL", "DEDUP", "X264", "BODYTRACK"}
# SPLASH-2(PARSECフレームワーク内のext/splash2から2026-07-06にビルドして追加):
# WATER_NSQUARED = lavaMDの代替(分子動力学N体、別実装のためlavaMD特有のクラッシュを
# 引き継がない想定)。RADIOSITY = タスクキュー型(work-stealing)並列、fork-join
# (NPB/GAPBS)・パイプライン(dedup/x264)とは違う第3の並列化パターン。
SPLASH2_WORKLOADS = {"WATER_NSQUARED", "RADIOSITY"}
# 標準入力からパラメータを読むワークロード(コマンドライン引数を持たない)
STDIN_WORKLOADS = {"WATER_NSQUARED"}

# PARSECワークロード名 → (バイナリを置いているディレクトリ, バイナリファイル名)
# fluidanimate以外(canneal/dedup/x264)は2026-07-06にPARSEC-3.0フレームワーク内の
# 既存ソースからビルドして追加(パイプライン並列・ロックフリー不規則アクセスという
# fork-join系(NPB/GAPBS)には無い軸を補うため)。
PARSEC_BINARY = {
    "FLUIDANIMATE": (FLUIDANIMATE_DIR, "fluidanimate"),
    "CANNEAL":      (CANNEAL_DIR, "canneal"),
    "DEDUP":        (DEDUP_DIR, "dedup"),
    "X264":         (X264_DIR, "x264"),
    "BODYTRACK":    (BODYTRACK_DIR, "bodytrack"),
}

# BENCH_CLASS → GAPBS グラフ頂点数スケール（2^g 頂点）
GAPBS_SCALE = {"S": 10, "W": 11, "A": 12, "B": 13, "C": 14, "D": 16}

# BENCH_CLASS → lavaMD boxes1d（総箱数 = boxes1d^3）
LAVAMD_BOXES = {"S": 3, "W": 4, "A": 5, "B": 8, "C": 10, "D": 15}

# PARSEC canneal/dedup/x264/fluidanimate の入力ファイル(PARSEC公式input_simsmall.tarから
# 展開、各src/inputs/に配置済み)。
#
# 相対パス("inputs/xxx")で持つ: 実行時cwdは常にバイナリ自身のディレクトリになる
# (sniper_sim_sid.pyはpodmanの-wでCONTAINER_BINに、sniper_sim_purple.pyはcdでbinary_dirに
# 移動する)ため、絶対パス(hiragahama上のホストパス)を渡すとコンテナ内やPurple上には
# 存在せず「file not found」で即終了してしまう(2026-07-06、fluidanimate導入時に発覚し、
# host-build時にすり替わっていたcanneal/dedup/x264でも同じ不具合を確認)。
X264_INPUT      = "inputs/eledream_640x360_8.y4m"

# 2026-07-06に「canneal/dedup/x264はbench_classを見ておらず、S/Wで完全に同じ実行に
# なっている」と発覚したが、この時点では`.gettsuushin.py`(Purple側の通信行列取得用
# スクリプト)側だけに個別のW用パラメータ(NSWAPS=40000、media_w.dat)を仮置きし、
# 実際にSniperを走らせるこちらの本線(get_binary_args)には反映されていなかった。
# SizeS実験を仕上げるにあたり2026-07-07に正式反映する。x264は同日の別調査で
# ワークロード自体を対象外としたため、クラス対応の対象外のまま。
#
# BENCH_CLASS → canneal 実行パラメータ(NSWAPS=焼きなましの反復回数、netlist=問題規模、
# nsteps)。
#   S: PARSEC公式simsmall.runconfと完全一致(10000, 100000.nets, 32)
#   W: 2026-07-06にユーザーが決めた独自スケール(NSWAPSのみ4倍、netlistは据え置き)。
#      既にこの定義でSniper実測・通信行列取得が完了しているため変更しない。
#   A: 2026-07-08、Purple上のJin/Agungの旧環境(sniper-detloc-backup)からPARSEC公式
#      input_simmedium.tar(200000.nets)を発見・取得し、PARSEC公式simmedium.runconfの
#      値をそのまま採用(15000, 200000.nets, 64)。Wと違い数式込みで公式値に準拠。
#   未知クラスはSにフォールバック。
CANNEAL_PARAMS = {
    "S": {"nswaps": 10000, "netlist": "inputs/100000.nets", "nsteps": 32},
    "W": {"nswaps": 40000, "netlist": "inputs/100000.nets", "nsteps": 32},
    "A": {"nswaps": 15000, "netlist": "inputs/200000.nets", "nsteps": 64},
}

# BENCH_CLASS → dedup 入力ファイル。
#   S: PARSEC公式simsmall基準のmedia.dat
#   W: media.datを2つ連結したmedia_w.dat(2026-07-06にPurple側で作成、独自スケール。
#      既にこの定義でSniper実測・通信行列取得が完了しているため変更しない)
#   A: 2026-07-08、Purple上のJin/Agungの旧環境からPARSEC公式input_simmedium.tar内の
#      media.dat(32MB、simsmallの約3倍)を取得しmedia_a.datとして配置。PARSEC公式の
#      本物のsimmediumサイズ(dedup公式runconfはファイル名でクラスを区別しておらず、
#      配布アーカイブ側でサイズが変わる方式だったため、アーカイブから直接取得)
#   未知クラスはSにフォールバック。
DEDUP_INPUT_BY_CLASS = {
    "S": "inputs/media.dat",
    "W": "inputs/media_w.dat",
    "A": "inputs/media_a.dat",
}

# BENCH_CLASS → bodytrack 実行パラメータ(シーケンスディレクトリ・フレーム数・
# 粒子数)。2026-07-09、Purple上のAgungさんの環境(sniper-detloc-backup系ではなく
# sniper-bench、PARSEC公式input_simsmall/simmedium/simlarge)からバイナリ
# (amd64-linux.gcc-pthreads版)と入力データを取得し追加。PARSEC公式仕様通り
# S/W/Aでシーケンス自体が異なる(facesimと違い本物のクラス分けが存在する)。
# カメラ数(4)・アニーリング層数(5)は全クラス共通、末尾のverbose(0)も固定。
BODYTRACK_PARAMS = {
    "S": {"sequence": "sequenceB_1", "frames": 1, "particles": 1000},
    "W": {"sequence": "sequenceB_2", "frames": 2, "particles": 2000},
    "A": {"sequence": "sequenceB_4", "frames": 4, "particles": 4000},
}
# fluidanimate: PARSEC公式input_simsmall.tarのin_35K.fluid(粒子数35K、Jinも使用実績あり)。
# 2026-07-06にsrc/inputs/へ配置。フレーム数はPARSEC標準simsmall相当の5。
FLUIDANIMATE_INPUT  = "inputs/in_35K.fluid"
FLUIDANIMATE_FRAMES = 5

# BENCH_CLASS → water_nsquared分子数(NMOLは完全立方数である必要がある)
WATER_NSQUARED_NMOL = {"S": 64, "W": 125, "A": 216, "B": 343, "C": 512, "D": 1000}

# BENCH_CLASS → GUPSテーブルサイズ指数(2^n個のuint64エントリ)。実行時argv[1]で
# コンパイル時デフォルト(LOG2_TABLESIZE=22)を上書きできる。Sクラス=22(32MB、
# シミュレートL3=4MBの8倍)を基準に1クラスごとに2倍ずつ増やす。
GUPS_LOG2_SIZE = {"S": 22, "W": 23, "A": 24, "B": 25, "C": 26, "D": 27}


def needs_stdin(workload: str) -> bool:
    """標準入力からパラメータを読むワークロードかどうか(water_nsquared等)。"""
    return workload.upper() in STDIN_WORKLOADS


def write_stdin_file(workload: str, bench_class: str, num_threads: int, out_dir: str) -> str:
    """
    標準入力からパラメータを読むワークロード用の入力ファイルをout_dir内に生成し、
    そのパスを返す。water.C冒頭コメント記載の10フィールド形式:
    TSTEP/NMOL/NSTEP/NORDER/NSAVE/NRST/NPRINT/NFMC/NumProcs/CUTOFF。
    NumProcsは実際のスレッド数に一致させる必要がある。
    """
    wl_upper = workload.upper()
    if wl_upper == "WATER_NSQUARED":
        nmol = WATER_NSQUARED_NMOL.get(bench_class, 64)
        content = f"1e-15\n{nmol}\n3\n6\n0\n0\n1\n0\n{num_threads}\n0\n"
        path = os.path.join(out_dir, "water_nsquared_input.txt")
        with open(path, "w") as f:
            f.write(content)
        return path
    raise ValueError(f"stdin不要のワークロード: {workload}")


def binary_path(workload: str, bench_class: str) -> str:
    """ワークロード名とクラスからバイナリの絶対パスを返す。"""
    wl_upper = workload.upper()
    if workload.lower() == "lavamd":
        return f"{LAVAMD_DIR}/lavaMD"
    if wl_upper in GAPBS_WORKLOADS:
        return f"{GAPBS_DIR}/{workload.lower()}"
    if wl_upper in PARSEC_WORKLOADS:
        directory, binary_name = PARSEC_BINARY[wl_upper]
        return f"{directory}/{binary_name}"
    if wl_upper == "WATER_NSQUARED":
        return f"{WATER_NSQUARED_DIR}/water_nsquared"
    if wl_upper == "RADIOSITY":
        return f"{RADIOSITY_DIR}/radiosity"
    if wl_upper in GUPS_WORKLOADS:
        return f"{GUPS_DIR}/gups"
    return f"{NPB_BIN_DIR}/{workload.lower()}.{bench_class}.x"


# canneal/dedupは起動時の`-t N`引数と実際に立ち上がるOSスレッド数が一致しない
# (パイプライン/コーディネータスレッドが追加で立つため。2026-07-07実測で確認:
# canneal `-t N` → 実N+1、dedup `-t N` → 実3N+3)。cpu_map・Job.num_threads・
# ホスト幅モデルなど、システム全体では一貫して「num_threads=実スレッド数」という
# 意味で扱う(他の全ワークロードと同じ規約)ため、get_binary_args()側でこの実
# スレッド数から逆算して正しい`-t`引数を組み立てる(2026-07-08、ユーザー提案の
# 「1引けばいい」を一般化: canneal=num_threads-1、dedup=(num_threads-3)//3)。
# dedupは3の倍数+3しか実現できない(6,9,12,15,...)ため、それ以外のnum_threadsを
# 渡すのは呼び出し側のミス→明示的にエラーにする。
def _dedup_arg_threads(num_threads: int) -> int:
    if (num_threads - 3) % 3 != 0 or num_threads < 6:
        raise ValueError(
            f"dedupはnum_threads=3n+3(6,9,12,15,...)でしか実現できません: {num_threads}"
        )
    return (num_threads - 3) // 3


def arg_threads_for(workload: str, num_threads: int) -> int:
    """
    num_threads(実スレッド数、システム全体で統一された意味)から、バイナリに
    渡す`-t`引数・comm.csvのファイル名(起動時引数ベースで命名されている)を
    逆算する。canneal/dedup以外は恒等写像。
    """
    wl = workload.upper()
    if wl == "CANNEAL":
        return num_threads - 1
    if wl == "DEDUP":
        return _dedup_arg_threads(num_threads)
    return num_threads


def resolve_valid_num_threads(workload: str, num_threads: int) -> int:
    """
    要求されたnum_threads(標準の2/8/12/16等)を、このワークロードで実現可能な
    最も近い値に変換する(dedupの3n+3制約など)。単純にスキップするのではなく
    「一番近い有効値」に丸めることで、dedupのようなワークロードでも標準の
    スレッド数リストから4点相当のカバレッジを得られるようにする
    (2026-07-10、ユーザー提案: 別バッチに分けるのではなくワークロードごとの
    変換をbuild_jobs()に組み込む方式)。他ワークロードでは恒等写像。
    """
    if workload.upper() == "DEDUP":
        rounded = round(num_threads / 3) * 3
        return max(6, rounded)
    return num_threads


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
    if wl_upper == "FLUIDANIMATE":
        # Usage: fluidanimate <threadnum> <framenum> <.fluid input file> [.fluid output file]
        return f"{num_threads} {FLUIDANIMATE_FRAMES} {FLUIDANIMATE_INPUT} /tmp/fluid_out_{num_threads}.fluid"
    if wl_upper == "CANNEAL":
        # Usage: canneal NTHREADS NSWAPS TEMP NETLIST NSTEPS。
        # NSWAPS/netlist/nstepsはbench_classでスケール(CANNEAL_PARAMS参照)。
        # 実スレッド数はNTHREADS+1になるため、num_threads(実スレッド数)から逆算する。
        p = CANNEAL_PARAMS.get(bench_class, CANNEAL_PARAMS["S"])
        arg_threads = arg_threads_for(workload, num_threads)
        return f"{arg_threads} {p['nswaps']} 2000 {p['netlist']} {p['nsteps']}"
    if wl_upper == "DEDUP":
        # PARSEC標準simsmall呼び出し(圧縮・パイプライン並列・verbose)。
        # 入力ファイルはbench_classでスケール(DEDUP_INPUT_BY_CLASS参照)。
        # 実スレッド数は3*(-t N)+3になるため、num_threads(実スレッド数)から逆算する。
        dedup_input = DEDUP_INPUT_BY_CLASS.get(bench_class, DEDUP_INPUT_BY_CLASS["S"])
        arg_threads = arg_threads_for(workload, num_threads)
        return f"-c -p -v -t {arg_threads} -i {dedup_input} -o /tmp/dedup_out_{arg_threads}.dat.ddp"
    if wl_upper == "X264":
        # PARSEC標準simsmall呼び出し
        return (
            "--quiet --qp 20 --partitions b8x8,i4x4 --ref 5 --direct auto "
            "--b-pyramid --weightb --mixed-refs --no-fast-pskip --me umh --subme 7 "
            f"--analyse b8x8,i4x4 --threads {num_threads} "
            f"-o /tmp/x264_out_{num_threads}.264 {X264_INPUT}"
        )
    if wl_upper == "BODYTRACK":
        # Usage: bodytrack <sequenceDir> <cameras> <frames> <particles> <layers> <verbose> <threads>
        # (PARSEC公式runconf準拠。入力ファイルはbench_classでスケール、BODYTRACK_PARAMS参照)
        p = BODYTRACK_PARAMS.get(bench_class, BODYTRACK_PARAMS["S"])
        return (
            f"inputs/{p['sequence']} 4 {p['frames']} {p['particles']} 5 0 {num_threads}"
        )
    if wl_upper == "RADIOSITY":
        # PARSEC標準simsmall呼び出し(バッチモード、標準roomシーン)
        return f"-batch -room -p {num_threads}"
    if wl_upper in GUPS_WORKLOADS:
        # スレッド数はOMP_NUM_THREADS環境変数(呼び出し側で設定済み)経由。
        # argv[1]はテーブルサイズ指数(2^n)のみ。
        log2_size = GUPS_LOG2_SIZE.get(bench_class, 22)
        return f"{log2_size}"
    if wl_upper == "WATER_NSQUARED":
        # 引数はコマンドラインではなく標準入力から渡す(write_stdin_file参照)
        return ""
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
    MPO は resolve_cpu_map() を使うこと（ここでは扱わない）。
    """
    if strategy not in STRATEGIES:
        print(f"[ERROR] 不明なストラテジー: {strategy}")
        sys.exit(1)
    return STRATEGIES[strategy]


def resolve_cpu_map(strategy: str, workload: str, bench_class: str, num_threads: int) -> list:
    """
    MPO を含む全ストラテジーに対応した cpu_map 解決の正本。
    MPO は Jin の本物の2段階アルゴリズム(utility.deloc_mapper: Step1通信局所性 +
    Step2ノード内Big/Small)で都度計算する（静的な推測ベースの MPO_MAPS はもう使わない）。
    それ以外の戦略は STRATEGIES の固定配置をそのまま返す。

    ultra_orchestrator.py / run.py / vsPOSM/vs_posm.py など、
    cpu_map 解決が必要な箇所は全てここを呼ぶ（各ファイルで個別に実装しない）。
    """
    if strategy == "MPO":
        from utility.deloc_mapper import compute_deloc_map_from_csv, find_comm_csv
        # comm.csvのファイル名は起動時の-t N(canneal/dedupではnum_threadsと不一致)
        # で決まるが、行列処理自体はnum_threads(実スレッド数)で行う必要がある。
        # パス探索はarg_threads_for()で逆算したnominal値、行列処理はnum_threads
        # (実スレッド数)で分ける。
        arg_threads = arg_threads_for(workload, num_threads)
        csv_path = find_comm_csv(workload, bench_class, arg_threads)
        cpu_map, _imbalance = compute_deloc_map_from_csv(csv_path, num_threads)
        return cpu_map
    return get_cpu_map(strategy, workload)


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


