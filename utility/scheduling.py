"""
scheduling.py
資源制約付きスケジューリング (Garey & Graham 1975, P|res 1|Cmax) の汎用アルゴリズム群。
ultra_orchestrator.py から2026-07-09に切り出した。Sniper/ワークロードの中身を
一切知らない、`duration`(実行時間)・`width`(資源消費量)属性を持つオブジェクトの
リストに対して動くだけの汎用スケジューリングロジック。

  - lpt_order: List Scheduling + LPT (長い順に、空いた瞬間に詰める)。
    最適解に対し (2 - 1/C) 以内の近似保証があり、ジョブ数が多くても高速。
  - cpsat_order: OR-Tools CP-SAT の cumulative 制約による厳密/近似解。
    ジョブ数が少ない場合のみ現実的(既定80件未満)。
  - _CapacityPool: 容量(実効コア数)を超えないよう、ジョブのwidthをゲートする
    貪欲リストスケジューラの資源プール。
"""

import heapq
import threading


def lpt_order(jobs: list) -> list:
    """List Scheduling + LPT: durationの長い順。jobsは`.duration`属性を持つこと。"""
    return sorted(jobs, key=lambda j: j.duration, reverse=True)


def estimate_makespan(ordered_jobs: list, capacity: float) -> float:
    """
    lpt_order/cpsat_orderで並べたジョブ列を、実際に投入する`_CapacityPool.acquire()`
    と全く同じ貪欲規則(「使用中がゼロでなく、かつ空き容量が足りない」場合だけ待つ
    ―単独ジョブがcapacityを超える幅を持っていても、他に何も走っていなければ
    そのまま開始できる)でシミュレートし、全ジョブ完了までの推定時間(秒)を返す。

    2026-07-11: 「スケジューリングが終わったら実行時間の見積もりをログに出して
    ほしい」というユーザー要望を受けて新設。ジョブ投入前(実行開始前)に
    呼び出す想定。live_load_fn/loadavg_fnによる実測ゲート(_CapacityPool側の
    第2安全弁)はシミュレートしない、静的モデルのみの見積もりなので、実行時は
    輻輳でこれより伸びる可能性がある点に注意。
    """
    if not ordered_jobs:
        return 0.0

    used = 0.0
    running: list[tuple[float, float]] = []  # (finish_time, width) のmin-heap
    now = 0.0
    for job in ordered_jobs:
        while running and used > 0 and used + job.width > capacity:
            finish_time, width = heapq.heappop(running)
            now = max(now, finish_time)
            used -= width
        used += job.width
        heapq.heappush(running, (now + job.duration, job.width))

    return max(finish_time for finish_time, _ in running)


def cpsat_order(jobs: list, capacity: float, time_limit_sec: float = 30.0) -> list | None:
    """
    OR-Tools CP-SAT の cumulative 制約で P|res 1|Cmax の厳密/近似解を求め、
    各ジョブの開始時刻順を返す。求解に失敗/タイムアウトした場合は None。
    jobsは`.duration`・`.width`属性を持つこと。
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
                live_load_fn=None, live_poll_interval: float = 10.0,
                loadavg_fn=None, loadavg_hard_limit: float | None = None):
        self.capacity = capacity
        # hard_limit/live_load_fn: 静的モデル(capacity)を通過した後の最終安全弁。
        # ワークロードごとのコストモデル(utility.capacity_model)未収載時のデフォルト値が
        # 実態より過小(2026-07-06に最大47%過小と判明)なケースに備え、投入直前に実測負荷を
        # 取得し、実測+width が物理コア数(hard_limit)を超えるなら投入を遅らせる。
        self.hard_limit = hard_limit
        self.live_load_fn = live_load_fn
        # loadavg_fn/loadavg_hard_limit: 2026-07-10のスケジューリング事故を受けて追加。
        # live_load_fn(podman stats等のCPU%)はメモリ帯域待ちでブロックされているスレッドを
        # 検知できない(CPU%は低いままload averageだけ急騰する)。load averageは実行待ち
        # キューの長さを直接反映するため、CPU%ゲートが見逃す種類の輻輳を捕捉できる、
        # 独立した第2の安全弁として並置する。widthを加算せず「現在の値」だけで判定する
        # (load averageは既に系全体の実行待ち状況を表す実測値であり、CPU%のような
        # 加算的な予測ではないため)。
        self.loadavg_fn = loadavg_fn
        self.loadavg_hard_limit = loadavg_hard_limit
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
                if self.loadavg_fn is not None and self.loadavg_hard_limit is not None:
                    self._cond.release()
                    try:
                        loadavg = self.loadavg_fn()
                    finally:
                        self._cond.acquire()
                    if loadavg > self.loadavg_hard_limit:
                        self._cond.wait(timeout=self.live_poll_interval)
                        continue
                self.used += width
                return

    def release(self, width: float) -> None:
        with self._cond:
            self.used -= width
            self._cond.notify_all()
