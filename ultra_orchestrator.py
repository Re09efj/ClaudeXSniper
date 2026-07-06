"""
ultra_orchestrator.py
資源制約付きスケジューリング (Garey & Graham 1975, P|res 1|Cmax) で、
全ワークロード×全戦略×全ベンチクラス×全スレッド数のジョブを一括投入し、
ホストの実効コア容量に対して厳密/近似スケジューリングして実行する。

orchestrator.py の設計上の欠陥（スレッド数ごとに完全逐次バッチ実行するため、
BT/SPのような重いジョブが同じバッチ内の軽いジョブの完了まで後続バッチを
ブロックする）を解消するのが目的。全ジョブを最初から1つのプールとして
スケジューリングし、空いた容量に次のジョブを即座に詰め込む。

このファイルは orchestrator.py に一切依存しない（orchestrator.py が将来
削除されても支障なく動く）。ワークロード一覧・戦略一覧・スレッド数・CLI引数の
指定方法は orchestrator.py と同じ流儀を踏襲しており、安定運用の実績が積めれば
本ファイルが orchestrator.py そのものを置き換える想定。

コストモデル (project_scheduling_model メモリ参照):
  - duration (壁時計時間): utility.run_profile の実測値/推定値
  - width (実消費ホストコア数%): ワークロード種別ごとの2THベースライン ×
    スレッド数スケーリング (cost(threads) ≈ baseline × (threads/2)^0.413)
    ワークロードの実行時間そのもの(研究上の性能比較)とは無関係な、
    Sniper自体のホストCPU消費効率の話である点に注意。

スケジューリング戦略:
  - 既定は List Scheduling + LPT (長い順に、空いた瞬間に詰める)。
    最適解に対し (2 - 1/C) 以内の近似保証があり、ジョブ数が多くても高速。
  - --exact 指定時、ジョブ数が少ない場合 (既定80件未満) のみ OR-Tools CP-SAT の
    cumulative 制約で厳密解を求め、その順序をLPTの代わりに使う。
    ジョブ数が多い場合は解けないため自動的にLPTにフォールバックする。

使い方:
  python3 ultra_orchestrator.py --threads 2 8 12 16 --bench-class S W
  python3 ultra_orchestrator.py --threads 8 --bench-class A --strategies Packed HPO
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from config.generate_config import generate_config, get_config_path
from sniper_sim               import run_sniper as run_sniper_sid
from sniper_sim_purple        import run_sniper as run_sniper_purple, SSH_HOST as PURPLE_SSH_HOST
from utility.cpu_affinity    import (resolve_cpu_map, binary_path, get_binary_args,
                                      save_affinity_config, needs_stdin, write_stdin_file)
from utility.csv_exporter    import export_csv
from utility.notify          import notify
from utility.power_model     import estimate as estimate_power
from utility.run_profile     import get_reference, update_from_run, estimate_walltime
from utility.stats_reader    import parse_node_stats

# ============================================================
# 実験設定（CLI 引数で上書き可能。orchestrator.py と同じ流儀）
# ============================================================
# GAPBS系は当初BFS/PR/BC/CC/SSSP/TCの6種だったが、_WIDTH_BASELINE_2THが全種
# 完全一致(57)することに加え、BFS/CC/SSSPは同じ「フロンティア型探索」で
# アクセスパターンがほぼ重複、BCもBFS往復2パスで重複度が高いと判断し、
# 質的に異なる3種(BFS=探索の基準形、PR=非探索の反復型、TC=集合演算型)に
# 絞った(2026-07-06)。lavaMDは未解決のクラッシュ問題(vsyscall修正後も別要因で
# クラッシュ)のため対象外として扱う。
# NPBはさらにCG(疎・不規則な間接参照 → GAPBS側で既にカバー済みの軸と重複)、
# SP(BTと同じADI法系統で通信パターンがほぼ同型、かつ全ワークロード中最重量
# スケーリング=180倍でコストも高い)を削り、4種(BT/FT/IS/MG)に絞った(2026-07-06)。
# PARSEC系(canneal=ロックフリー不規則アクセス、dedup/x264=パイプライン並列)を
# 新たに追加し、fork-join(NPB/GAPBS)には無い並列化パターンの軸を補った。
# 粒子シミュレーション系(分子動力学/流体)はwater_nsquared(SPLASH-2、静的all-pairs
# N体)のみ採用する。fluidanimate(PARSEC、動的グリッドSPH)はJin実績ありだったが、
# SID本番イメージへのGAPBS修正(sendInstructionタッチガード)適用後も同じ
# SIGSEGVで再現性よくクラッシュすることを確認したため不採用。lavaMD(Rodinia)も
# 別クラッシュで既に対象外であり、この系統(グリッド細分割+境界セル同期を伴う
# 粒子シミュレーション)はこのSniper/Pin環境下では実行不能と判断し、Rodinia全種を
# 対象外として扱う(2026-07-06)。water_nsquaredは全スレッドクラッシュなく完走
# 済みのためこちらを採用。
# GAPBS BFS/TCは実測(run_profile.json)で全(class,threads)組み合わせにおいて
# TCが命令数・壁時計時間ともに常にBFSを上回る(TCはBFS相当の下地計算＋三角形
# カウントの追加処理を含むため)ことを確認し、より軽いBFSを基準形として残し、
# 重複度の高いTCを削除しようとしたが(2026-07-06)、直後にBFS/PR自体がSID上で
# sendInstruction系SIGSEGV(GAPBS修正適用後の本番イメージでも再現、crashアドレス
#下3桁が複数ワークロードで一致=未修正の別コードパスの可能性)で再発することが
# 判明。原因未特定のため、GAPBS系統(BFS/PR/TC/BC/CC/SSSP)をすべて対象外とした
# (2026-07-06)。
# 代わりにGUPS(HPCC RandomAccess)を追加。テーブルサイズがシミュレート対象L3
# (config/generate_config.py参照、i7-1195G7ベースで12MB/ノード)を大きく超える
# 純粋ランダムアクセスカーネルで、計算をほぼ持たないためcanneal(ランダムだが
# 実コスト計算を伴う)とも異なる、NUMA相互接続そのものの効果を測る基準点。
WORKLOADS = ["BT", "FT", "IS", "MG",
             "canneal", "dedup", "x264",
             "WATER_NSQUARED", "GUPS"]

# 戦略。既存5戦略(Packed/Scatter/HPO/EPO/MPO)に加え、AKARIN候補生成を意味する
# 特別な戦略トークン akarin_h / akarin_l を混在指定できる。
# AKARIN候補はcpu_mapそのものを変える「戦略」であってワークロードの種類ではない
# (WORKLOADSではなくこちらに属する)。
#   akarin_h: WORKLOADSのうち重量級(NPB系BT/FT/IS/MG)だけを対象に、
#             粗いalphaグリッド(3点)でAKARIN候補cpu_mapを生成・実行
#   akarin_l: WORKLOADSのうち軽量級(canneal/dedup/x264/WATER_NSQUARED/GUPS)
#             だけを対象に、密なalphaグリッド(21点)でAKARIN候補cpu_mapを生成・実行
STRATEGIES_TO_RUN = ["Packed", "Scatter", "HPO", "EPO", "MPO"]
THREAD_COUNTS     = [2, 8, 12, 16]
BENCH_CLASSES     = ["S", "W"]

# 実行先マシン。STRATEGIES_TO_RUNと同じ長さの配列で位置対応させる
# (WORKLOADS/THREAD_COUNTSと同じ「配列を書き換えるだけ」の感覚でマシンを切り替えられる)。
#   MACHINE = ["sid"]                                        → 全戦略をsid(hiragahama)で実行 (既定)
#   MACHINE = ["purple"]                                     → 全戦略をpurpleで実行
#   MACHINE = ["sid","sid","sid","sid","sid","purple","purple"]
#     (STRATEGIES_TO_RUN = [...5戦略..., "akarin_h", "akarin_l"] と同じ長さ)
#     → 既存5戦略はsid、AKARIN候補生成(重量級/軽量級とも)はpurpleで実行
MACHINE = ["sid"]

AKARIN_STRATEGY_TOKENS = {"akarin_h", "akarin_l"}

SID_CAPACITY_DEFAULT    = 21.0  # hiragahama(hostname: sid) 実効コア数上限
PURPLE_CAPACITY_DEFAULT = 45.0  # Purpleは56論理コアの共有サーバ、生スレッド数ベースで上限45(2026-07-05: 50→45に削減)

# 2026-07-06のスケジューリング事故(load average 55超過)を受けて追加した
# ハード上限。_WIDTH_BASELINE_2TH未収載のワークロードはhost_width_pct()の
# デフォルト(100%)にフォールバックするため、静的モデルだけでは実際の消費を
# 過小評価しうる。live_*_load_cores() で実測した「今まさに使われているコア数」
# を踏まえ、これを超える場合はジョブ投入を遅らせるゲートを _CapacityPool に追加した。
# 静的モデル(LPT/CP-SAT)によるジョブの「順序付け」は従来通り使い、この実測ゲートは
# 投入の可否だけを最終チェックする安全弁という位置づけ(ハイブリッド方式)。
SID_HARD_LIMIT_CORES    = 24.0  # SIDの物理コア数そのもの
PURPLE_HARD_LIMIT_CORES = 56.0  # Purpleの論理コア数そのもの

CLAUDEXSNIPER_DIR = "/home/hiragahama/ClaudeXSniper"
OUTPUT_BASE_TMPL  = "/home/hiragahama/ClaudeXSniper/Outputs/size{cls}"
VALID_THREAD_COUNTS = {2, 4, 6, 8, 12, 16, 32}
VALID_BENCH_CLASSES = {"S", "W", "A", "B", "C", "D"}
VALID_MACHINES = {"sid", "purple"}  # sid = hiragahama(このホストの実際のhostname)

# マシンごとの run_sniper 実装 (マシン切替機構)
RUN_SNIPER_BACKENDS = {
    "sid":    run_sniper_sid,
    "purple": run_sniper_purple,
}

TIMEOUT_LOG = os.path.join(CLAUDEXSNIPER_DIR, "logs", "timeoutwl.log")


def _log_timeout(workload: str, strategy: str, bench_class: str, num_threads: int,
                 elapsed: float, timeout_sec: float, attempt: int, max_retries: int) -> None:
    outcome = f"retry({attempt + 2}/{max_retries + 1})" if attempt < max_retries else "FAILED"
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
        f"{workload:<8} {strategy:<8} Class={bench_class}  {num_threads}TH  "
        f"elapsed={elapsed:.0f}s  timeout={timeout_sec:.0f}s  {outcome}  [ultra]\n"
    )
    os.makedirs(os.path.dirname(TIMEOUT_LOG), exist_ok=True)
    with open(TIMEOUT_LOG, "a") as f:
        f.write(line)


def _validate(thread_list: list[int], class_list: list[str]) -> None:
    bad_threads = [t for t in thread_list if t not in VALID_THREAD_COUNTS]
    bad_classes = [c for c in class_list  if c not in VALID_BENCH_CLASSES]
    errors = []
    if bad_threads:
        errors.append(f"不正なスレッド数: {bad_threads}  有効値: {sorted(VALID_THREAD_COUNTS)}")
    if bad_classes:
        errors.append(f"不正なベンチクラス: {bad_classes}  有効値: {sorted(VALID_BENCH_CLASSES)}")
    if errors:
        for msg in errors:
            print(f"[ERROR] {msg}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# コストモデル
# ============================================================

# ワークロード種別ごとの実消費ホストコア(%) @ 2TH実測 (project_scheduling_model参照)
# canneal/dedup/x264/WATER_NSQUARED/GUPSはまだ実測が無いため未記載 →
# host_width_pct()のデフォルト値(100)にフォールバックする。実測が取れ次第
# ここに追記すること。GAPBS系(BFS/PR/TC/BC/CC/SSSP)は2026-07-06に
# クラッシュ再発のためワークロード自体を削除。
_WIDTH_BASELINE_2TH = {
    "BT": 133, "FT": 117, "IS": 102, "MG": 100,
}
_WIDTH_EXPONENT = 0.413  # BTの実測4点フィット cost(threads)≈99.6×threads^0.413 の指数部を流用


def host_width_pct(workload: str, num_threads: int) -> float:
    """このワークロード・スレッド数がホストの実コアを何%消費するかの推定値。"""
    baseline = _WIDTH_BASELINE_2TH.get(workload, 100)
    scale = (num_threads / 2) ** _WIDTH_EXPONENT
    return baseline * scale


def live_sid_load_cores() -> float:
    """
    現時点でSID上の全podmanコンテナが実際に消費しているCPUコア数の実測値。
    podman stats の CPUPerc (%) 合計を100で割ってコア単位に変換する。
    取得に失敗した場合は安全側(0.0=制限なし扱い)に倒す。
    """
    try:
        out = subprocess.run(
            ["podman", "stats", "--no-stream", "--format", "{{.CPUPerc}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        total_pct = sum(float(l.strip().rstrip("%")) for l in out.splitlines() if l.strip())
        return total_pct / 100.0
    except Exception:
        return 0.0


def live_purple_load_cores() -> float:
    """
    現時点でPurple上の全プロセスが実際に消費しているCPUコア数の実測値。
    SSH経由で ps -eo pcpu の合計を取得しコア単位に変換する。
    SSH自体が不調な場合(2026-07-06に発生した接続不能など)も安全側(0.0)に倒す。
    """
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
             PURPLE_SSH_HOST, "ps -eo pcpu --no-headers"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        total_pct = sum(float(l.strip()) for l in out.splitlines() if l.strip())
        return total_pct / 100.0
    except Exception:
        return 0.0


def job_duration_sec(workload: str, bench_class: str, num_threads: int, machine: str = "sid") -> float:
    """
    壁時計時間の推定 (実測があれば実測、無ければ utility.run_profile の推定式)。
    machineごとに参照キーが分離されている(sid/purpleでは実測walltimeが系統的に
    2〜3倍異なることが判明したため、混同しないよう2026-07-05に分離)。
    """
    ref = get_reference(workload, bench_class, num_threads, machine)
    if ref:
        return ref["wallTime"]
    est = estimate_walltime(workload, bench_class, num_threads, machine)
    return est if est is not None else 3600.0  # 完全に未知なら1時間と仮定


# ============================================================
# ジョブ定義とスケジューリング
# ============================================================

class Job:
    __slots__ = ("workload", "strategy", "bench_class", "num_threads", "width", "duration",
                 "backend", "cpu_map", "provenance")

    def __init__(self, workload, strategy, bench_class, num_threads,
                backend="sid", cpu_map=None, provenance=None):
        self.workload    = workload
        self.strategy    = strategy
        self.bench_class = bench_class
        self.num_threads = num_threads
        self.backend     = backend
        # cpu_map を明示指定した場合はそれを使う (AKARIN候補ジョブ)。
        # None なら run_job 側で strategy 名から resolve_cpu_map する (既存5戦略ジョブ)。
        self.cpu_map     = cpu_map
        # 元となった候補ラベル一覧 (AKARINジョブの由来記録。affinity_config.txtに残す)
        self.provenance  = provenance
        self.duration    = job_duration_sec(workload, bench_class, num_threads, backend)

        if backend == "purple":
            # Purpleは実消費コア%の重み付けモデルを持たないため、生スレッド数を
            # そのままcapacity単位として使う (実使用スレッド数=num_threadsは不変)。
            self.width = float(num_threads)
        else:
            # width はコア等価数 (capacity と同じ単位)。host_width_pct は%を返すので /100 する。
            self.width = host_width_pct(workload, num_threads) / 100.0

    def __repr__(self):
        return (f"Job({self.workload}/{self.strategy}/{self.bench_class}/"
                f"{self.num_threads}TH@{self.backend}, w={self.width:.2f}, d={self.duration:.0f}s)")


def _build_akarin_jobs_for_workload(workload: str, bench_class: str, num_threads: int,
                                    backend: str) -> list[Job]:
    """
    1つのワークロードについて、akarin.generate_candidates で計算した候補cpu_mapの
    うち既存5戦略(Packed/Scatter/HPO/EPO/MPO)のいずれとも一致しないもの
    (=alphaラベルを持つ候補)だけをJob化する。完全に既存戦略と同一の候補は
    既存パイプラインでカバー済みなので、ここでは重複実行しない。
    """
    from akarin.generate_candidates import generate_candidates

    jobs = []
    candidates = generate_candidates(workload, bench_class, num_threads)
    for idx, entry in enumerate(candidates.values()):
        labels = entry["labels"]
        alpha_labels = [l for l in labels if l.startswith("alpha=")]
        if not alpha_labels:
            continue  # 既存5戦略と完全一致 → スキップ(重複実行防止)
        rep = alpha_labels[0].split("=", 1)[1].replace(".", "")
        label = f"AKARIN_a{rep}_{idx}"
        jobs.append(Job(workload, label, bench_class, num_threads, backend=backend,
                        cpu_map=entry["cpu_map"], provenance=labels))
    return jobs


def build_jobs(workloads, strategies, bench_classes, thread_counts, backend="sid") -> list[Job]:
    """
    strategies には既存5戦略(Packed/Scatter/HPO/EPO/MPO)に加え、AKARIN候補生成を
    意味する特別な戦略トークン akarin_h(重量級ワークロードのみ対象、粗いalphaグリッド)
    / akarin_l(軽量級ワークロードのみ対象、密なalphaグリッド)を混在指定できる。
    重量級/軽量級の分類は akarin.generate_candidates.HEAVY_WORKLOADS に従う。
    """
    from akarin.generate_candidates import HEAVY_WORKLOADS as AKARIN_HEAVY_WORKLOADS

    jobs = []
    for cls in bench_classes:
        for th in thread_counts:
            for st in strategies:
                if st == "akarin_h":
                    for wl in workloads:
                        if wl in AKARIN_HEAVY_WORKLOADS:
                            jobs.extend(_build_akarin_jobs_for_workload(wl, cls, th, backend))
                elif st == "akarin_l":
                    for wl in workloads:
                        if wl not in AKARIN_HEAVY_WORKLOADS:
                            jobs.extend(_build_akarin_jobs_for_workload(wl, cls, th, backend))
                else:
                    for wl in workloads:
                        jobs.append(Job(wl, st, cls, th, backend=backend))
    return jobs


def lpt_order(jobs: list[Job]) -> list[Job]:
    """List Scheduling + LPT: durationの長い順。"""
    return sorted(jobs, key=lambda j: j.duration, reverse=True)


def cpsat_order(jobs: list[Job], capacity: float, time_limit_sec: float = 30.0) -> list[Job] | None:
    """
    OR-Tools CP-SAT の cumulative 制約で P|res 1|Cmax の厳密/近似解を求め、
    各ジョブの開始時刻順を返す。求解に失敗/タイムアウトした場合は None。
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None

    model = cp_model.CpModel()
    horizon = int(sum(j.duration for j in jobs)) + 1
    starts, ends, intervals = [], [], []
    for j in jobs:
        dur = max(int(j.duration), 1)
        start = model.NewIntVar(0, horizon, "start")
        end   = model.NewIntVar(0, horizon, "end")
        interval = model.NewIntervalVar(start, dur, end, "interval")
        starts.append(start)
        ends.append(end)
        intervals.append(interval)

    # width はコア単位の小数 (例: 1.33) なので、CP-SAT の整数制約用に100倍して丸める
    _SCALE = 100
    demands = [max(int(round(j.width * _SCALE)), 1) for j in jobs]
    model.AddCumulative(intervals, demands, int(capacity * _SCALE))

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, ends)
    model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    order = sorted(range(len(jobs)), key=lambda i: solver.Value(starts[i]))
    return [jobs[i] for i in order]


class _CapacityPool:
    """容量(実効コア数)を超えないよう、ジョブのwidthをゲートする貪欲リストスケジューラの資源プール。"""

    def __init__(self, capacity: float, hard_limit: float | None = None,
                live_load_fn=None, live_poll_interval: float = 10.0):
        self.capacity = capacity
        # hard_limit/live_load_fn: 静的モデル(capacity)を通過した後の最終安全弁。
        # _WIDTH_BASELINE_2TH未収載ワークロードのデフォルト値(100%)が実態より
        # 過小(2026-07-06に最大47%過小と判明)なケースに備え、投入直前に実測負荷を
        # 取得し、実測+width が物理コア数(hard_limit)を超えるなら投入を遅らせる。
        self.hard_limit = hard_limit
        self.live_load_fn = live_load_fn
        self.live_poll_interval = live_poll_interval
        self.used = 0.0
        self._cond = threading.Condition()

    def acquire(self, width: float) -> None:
        with self._cond:
            while True:
                if self.used > 0 and self.used + width > self.capacity:
                    self._cond.wait()
                    continue
                if self.live_load_fn is not None and self.hard_limit is not None:
                    # ロックを保持したままだと実測(podman stats/SSH)の待ち時間分
                    # 他ジョブの release() が遅延するため、いったん解放して計測する。
                    self._cond.release()
                    try:
                        live = self.live_load_fn()
                    finally:
                        self._cond.acquire()
                    if live + width > self.hard_limit:
                        # 静的モデルは通過したが実測が逼迫している → TOCTOU回避のため
                        # 即座には確保せず、一定時間待って実測ごと再チェックする
                        # (このwait中に他ジョブが完了して実測が下がる可能性がある)。
                        self._cond.wait(timeout=self.live_poll_interval)
                        continue
                self.used += width
                return

    def release(self, width: float) -> None:
        with self._cond:
            self.used -= width
            self._cond.notify_all()


# ============================================================
# ジョブ実行 (orchestrator.py の run_one 相当。タイムアウト・リトライも踏襲)
# ============================================================

def run_job(job: Job, run_id: str, no_timeout: bool = False) -> str | None:
    output_base = OUTPUT_BASE_TMPL.format(cls=job.bench_class)
    cpu_map  = job.cpu_map if job.cpu_map is not None else resolve_cpu_map(
        job.strategy, job.workload, job.bench_class, job.num_threads)
    bin_path = binary_path(job.workload, job.bench_class)
    bin_args = get_binary_args(job.workload, job.bench_class, job.num_threads)
    run_sniper = RUN_SNIPER_BACKENDS[job.backend]

    out_dir = os.path.join(
        output_base, f"{job.num_threads}TH",
        f"{job.workload}_{job.bench_class}_{job.strategy}_{job.num_threads}TH_{run_id}",
    )

    ref          = get_reference(job.workload, job.bench_class, job.num_threads, job.backend)
    expected_sec = ref["wallTime"] if ref else job.duration
    timeout_sec  = float("inf") if no_timeout else max(expected_sec * 3, 600)

    MAX_RETRIES = 1
    elapsed = 0.0
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        cfg_path = get_config_path(out_dir, job.strategy, job.num_threads)
        generate_config(job.strategy, job.num_threads, cpu_map, cfg_path)
        save_affinity_config(out_dir, job.strategy, job.workload, job.bench_class,
                             cpu_map, job.num_threads)
        stdin_path = None
        if needs_stdin(job.workload):
            stdin_path = write_stdin_file(job.workload, job.bench_class, job.num_threads, out_dir)
        if job.provenance:
            with open(os.path.join(out_dir, "affinity_config.txt"), "a") as f:
                f.write(f"\n# AKARIN候補由来: {', '.join(job.provenance)}\n")
                f.write(f"# backend={job.backend}\n")

        log_path = os.path.join(out_dir, "sniper.log")
        log_file = open(log_path, "w")

        start       = time.time()
        done_flag   = [False]
        ret_code    = [None]
        proc_holder = []
        timed_out   = [False]

        def _do_run():
            ret_code[0] = run_sniper(
                binary_path=bin_path, binary_args=bin_args,
                num_threads=job.num_threads, cpu_map=cpu_map,
                strategy=job.strategy, output_dir=out_dir,
                config_path=cfg_path, log_file=log_file,
                workload=job.workload, proc_holder=proc_holder,
                stdin_path=stdin_path,
            )
            done_flag[0] = True

        t = threading.Thread(target=_do_run, daemon=True)
        t.start()
        while not done_flag[0]:
            elapsed = time.time() - start
            if elapsed > timeout_sec and proc_holder:
                timed_out[0] = True
                proc_holder[0].kill()
                break
            time.sleep(1)
        t.join()
        log_file.close()
        elapsed = time.time() - start

        if timed_out[0]:
            _log_timeout(job.workload, job.strategy, job.bench_class, job.num_threads,
                        elapsed, timeout_sec, attempt, MAX_RETRIES)
            if attempt < MAX_RETRIES:
                continue
            print(f"[TIMEOUT] {job} ({elapsed:.0f}s > {timeout_sec:.0f}s)", flush=True)
            return None

        if ret_code[0] != 0:
            # ret=255はsniper_sim_purple.pyのSSH/scp起動失敗を意味することが多い。
            # Purple向けジョブを大量に同時起動するとsshdのMaxStartups(既定10:30:100)
            # に引っかかり接続がランダムに拒否されることがあると2026-07-06に判明
            # (起動直後のバーストでのみ発生、数秒後には自然に収まる一過性の事象)。
            # タイムアウトと同様にリトライする。
            print(f"[ERROR] {job} 失敗 ret={ret_code[0]}", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(3)
                continue
            return None
        break

    power = estimate_power(out_dir, cpu_map, job.num_threads)
    update_from_run(job.workload, job.bench_class, job.num_threads, out_dir, elapsed, job.backend)
    export_csv(out_dir, job.workload, job.strategy, job.bench_class, job.num_threads, cpu_map, power)
    return out_dir


def _run_and_release(job: Job, pool: _CapacityPool, run_id: str, no_timeout: bool,
                     counters: dict, lock: threading.Lock):
    try:
        out_dir = run_job(job, run_id, no_timeout=no_timeout)
        with lock:
            counters["done"] += 1
            status = "OK" if out_dir else "FAILED"
            print(f"[{counters['done']}/{counters['total']}] {job} -> {status}", flush=True)
    finally:
        pool.release(job.width)


def _order_jobs(jobs: list[Job], capacity: float, use_exact: bool, exact_threshold: int) -> list[Job]:
    if use_exact and len(jobs) < exact_threshold:
        print(f"[SCHED] CP-SAT厳密スケジューリングを試行 ({len(jobs)}件)...")
        ordered = cpsat_order(jobs, capacity)
        if ordered is None:
            print("[SCHED] CP-SATが時間内に解けず、LPTにフォールバック")
            ordered = lpt_order(jobs)
        else:
            print("[SCHED] CP-SAT厳密解を採用")
    else:
        if use_exact:
            print(f"[SCHED] ジョブ数{len(jobs)}件はCP-SATには多すぎるため、LPTを使用")
        ordered = lpt_order(jobs)
    return ordered


def _run_pool(jobs: list[Job], capacity: float, run_id: str, use_exact: bool,
             no_timeout: bool, exact_threshold: int, counters: dict, lock: threading.Lock,
             hard_limit: float | None = None, live_load_fn=None) -> None:
    if not jobs:
        return
    ordered = _order_jobs(jobs, capacity, use_exact, exact_threshold)
    pool = _CapacityPool(capacity, hard_limit=hard_limit, live_load_fn=live_load_fn)
    threads = []
    for job in ordered:
        pool.acquire(job.width)
        t = threading.Thread(target=_run_and_release,
                             args=(job, pool, run_id, no_timeout, counters, lock))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def schedule_and_run(jobs: list[Job], capacity: float, run_id: str, use_exact: bool,
                     no_timeout: bool = False, exact_threshold: int = 80,
                     purple_capacity: float = PURPLE_CAPACITY_DEFAULT) -> None:
    """
    backend("sid"/"purple")ごとに独立した資源プールでスケジューリングする
    (マシン切替機構)。hiragahama(sid)とPurpleは別々の物理資源なので、互いを
    待たせず完全並行に実行する(2マシン並列スケジューリングは、backendごとに
    独立した_CapacityPoolを別スレッドで走らせるだけで実現できる)。
    """
    sid_jobs    = [j for j in jobs if j.backend == "sid"]
    purple_jobs = [j for j in jobs if j.backend == "purple"]

    counters = {"done": 0, "total": len(jobs)}
    lock = threading.Lock()

    print(f"\n{'='*64}")
    print(f"  ULTRA ORCHESTRATOR — 資源制約付きスケジューリング実行")
    print(f"  ジョブ数={len(jobs)}  (sid={len(sid_jobs)}, purple={len(purple_jobs)})")
    print(f"  実効容量: sid={capacity}コア  purple={purple_capacity}スレッド")
    print(f"{'='*64}\n")

    group_threads = [
        threading.Thread(target=_run_pool, args=(
            sid_jobs, capacity, run_id, use_exact, no_timeout, exact_threshold, counters, lock,
            SID_HARD_LIMIT_CORES, live_sid_load_cores)),
        threading.Thread(target=_run_pool, args=(
            purple_jobs, purple_capacity, run_id, use_exact, no_timeout, exact_threshold, counters, lock,
            PURPLE_HARD_LIMIT_CORES, live_purple_load_cores)),
    ]
    for t in group_threads:
        t.start()
    for t in group_threads:
        t.join()

    print(f"\n[完了] {counters['done']}/{counters['total']} 件処理")


def resolve_strategy_machine_pairs(strategy_tokens: list[str], machine_tokens: list[str]) -> list[tuple[str, str]]:
    """
    戦略トークン(Packed/Scatter/HPO/EPO/MPO/akarin_h/akarin_l)とマシントークンを
    位置対応でペアにする。CLI(--strategies/--machine)からもPythonコードから
    STRATEGIES_TO_RUN/MACHINE定数を直接書き換えて使っても同じ解決ロジックになる。
      例: resolve_strategy_machine_pairs(
              ["Packed","Scatter","HPO","EPO","MPO","akarin_h","akarin_l"],
              ["sid","sid","sid","sid","sid","purple","purple"])
          → 既存5戦略はsid、AKARIN候補生成(重量級/軽量級とも)はpurple
      machine_tokens が1個だけなら全strategyトークンに一律適用。
      複数指定時はstrategyトークン数と個数が一致している必要がある(位置対応)。
    """
    if len(machine_tokens) == 1:
        machine_tokens = machine_tokens * len(strategy_tokens)
    if len(machine_tokens) != len(strategy_tokens):
        raise ValueError(
            f"machine の個数({len(machine_tokens)})が strategies の個数"
            f"({len(strategy_tokens)})と一致しません(1個 or 同数を指定してください)")
    return list(zip(strategy_tokens, machine_tokens))


def group_pairs_by_machine(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    by_machine: dict[str, list[str]] = {}
    for token, machine in pairs:
        by_machine.setdefault(machine, []).append(token)
    return by_machine


# ============================================================
# CLI (orchestrator.py と同じ引数名・流儀)
# ============================================================

def _parse_args():
    p = argparse.ArgumentParser(description="ClaudeXSniper ultra orchestrator (厳密/近似スケジューリング版)")
    p.add_argument("--threads",     type=int, nargs="+", default=None,
                   help="スレッド数 (複数指定可: --threads 2 8 12 16)")
    p.add_argument("--bench-class", nargs="+", default=None,
                   choices=sorted(VALID_BENCH_CLASSES),
                   help="ベンチクラス (複数指定可: --bench-class S W。省略時: BENCH_CLASSES)")
    p.add_argument("--strategies",  nargs="+", default=None,
                   help="戦略名(Packed/Scatter/HPO/EPO/MPO)、またはAKARIN候補生成トークン"
                        "(akarin_h=重量級ワークロード対象/粗グリッド, akarin_l=軽量級ワークロード対象/密グリッド)。"
                        "複数指定可: --strategies Packed MPO akarin_h akarin_l")
    p.add_argument("--workloads",   nargs="+", default=None)
    p.add_argument("--machine",     nargs="+", default=None, choices=sorted(VALID_MACHINES),
                   help="実行先マシン(マシン切替機構)。1個なら全--strategiesに一律適用、"
                        "複数なら--strategiesと同数で位置対応 (例: --strategies Packed akarin_h "
                        "--machine sid purple)。省略時: MACHINE定数")
    p.add_argument("--capacity",    type=float, default=None,
                   help=f"sid(hiragahama)実効コア数上限 (省略時: {SID_CAPACITY_DEFAULT})")
    p.add_argument("--purple-capacity", type=float, default=None,
                   help=f"Purple実効スレッド数上限 (省略時: {PURPLE_CAPACITY_DEFAULT})")
    p.add_argument("--exact",       action="store_true",
                   help="ジョブ数が少なければCP-SAT厳密解を試す (既定はLPT)")
    p.add_argument("--no-timeout",  action="store_true",
                   help="タイムアウトを無効化 (sizeA など長時間実験用)")
    return p.parse_args()


def main():
    args = _parse_args()

    thread_list = args.threads     if args.threads     is not None else THREAD_COUNTS
    class_list  = args.bench_class if args.bench_class is not None else BENCH_CLASSES
    workloads   = args.workloads   or WORKLOADS
    strategy_tokens = args.strategies or STRATEGIES_TO_RUN
    machine_tokens  = args.machine    or MACHINE
    capacity    = args.capacity    if args.capacity    is not None else SID_CAPACITY_DEFAULT
    purple_capacity = args.purple_capacity if args.purple_capacity is not None else PURPLE_CAPACITY_DEFAULT

    _validate(thread_list, class_list)

    try:
        pairs = resolve_strategy_machine_pairs(strategy_tokens, machine_tokens)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    by_machine = group_pairs_by_machine(pairs)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    jobs: list[Job] = []
    for machine, st_list in by_machine.items():
        machine_jobs = build_jobs(workloads, st_list, class_list, thread_list, backend=machine)
        jobs.extend(machine_jobs)
        print(f"[JOBS] {len(machine_jobs)}件 (machine={machine}, strategies={st_list}, "
              f"workloads={len(workloads)}, bench_class={class_list}, threads={thread_list})")

    notify(
        f"[ultra_orchestrator] 実行開始  class={class_list}  threads={thread_list}  "
        f"strategies/machine={pairs}  workloads={workloads}  capacity={capacity}  "
        f"purple_capacity={purple_capacity}  no_timeout={args.no_timeout}"
    )

    schedule_and_run(jobs, capacity, run_id, use_exact=args.exact, no_timeout=args.no_timeout,
                     purple_capacity=purple_capacity)

    notify(f"[ultra_orchestrator] 完了  class={class_list}  threads={thread_list}")


if __name__ == "__main__":
    main()
