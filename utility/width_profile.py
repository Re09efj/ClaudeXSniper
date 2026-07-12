"""
width_profile.py
実行中のPodmanコンテナのCPU%を収束検知方式で計測し、utility.capacity_model の
静的モデル(host_width_pct)を実測で補正するデータベース(Data/width_profile.json)
を構築する。

project_scheduling_model メモリの知見:
  - SizeS/Wは起動後約30秒で定常状態に収束
  - SizeAは最大400秒(約6.7分)かけて収束
  - 固定時間で1回だけ測ると、クラスやワークロードによって「まだ収束していない
    低めの値」を掴む(2026-07-04に実際に踏んだ罠)
これを踏まえ、固定時間待つのではなく「直近N回のポーリング値の変動が閾値以下に
なったら収束とみなす」収束検知方式を採用する(2026-07-12)。
"""

import json
import os
import subprocess
import threading
import time

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "Data", "width_profile.json")
_lock = threading.Lock()

_POLL_INTERVAL = 60.0   # ポーリング間隔(秒、2026-07-12: 10→60に変更、ユーザー指定)
_WINDOW        = 3      # 収束判定に使う直近サンプル数
_TOLERANCE     = 0.05   # 直近WINDOW件の(max-min)/meanがこの値以下なら収束とみなす
_MAX_WAIT      = 600.0  # これ以上待っても収束しなければ諦める(秒、SizeA想定で余裕を持たせた)
_WARMUP_SKIP   = 2      # 起動直後のコンテナ立ち上げ・Pin計装フェーズのノイズを無視するため、
                         # 最初のNサンプルは収束判定に使わない


def _key(workload: str, bench_class: str, num_threads: int, machine: str = "sid") -> str:
    base = f"{workload}_{bench_class}_{num_threads}"
    return base if machine == "sid" else f"{base}@{machine}"


def _load() -> dict:
    if not os.path.exists(_PROFILE_PATH):
        return {}
    try:
        with open(_PROFILE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_PROFILE_PATH), exist_ok=True)
    tmp = _PROFILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, _PROFILE_PATH)


def _sample_cpu_pct(container_name: str) -> float | None:
    """podman statsで指定コンテナの瞬間CPU%を取得する。コンテナが存在しなければNone。"""
    try:
        out = subprocess.run(
            ["podman", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        line = out.stdout.strip()
        if not line:
            return None
        return float(line.rstrip("%"))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _sample_cpu_pct_remote(pgid_file: str, ssh_host: str, ssh_opts: list[str]) -> float | None:
    """
    SSH経由でPurple上のプロセスグループ(.remote_pgidファイルに記録済みのPGID)を
    調べ、そのグループ全体(run-sniperラッパー・sniper本体・Pin配下で動く実際の
    ワークロードバイナリ等)のCPU%合計を取得する。SID側(podman stats)がコンテナ
    全体の合計CPU%を見ているのと測定基準を揃えるため、sniper本体単体ではなく
    グループ全体を合算する(2026-07-12: 実機確認でsniper本体142%とは別にPin配下の
    実バイナリ(例: mg.W.x)が21%消費しているのを確認、単体だと過小評価になる)。
    プロセスグループがまだ存在しない/既に消滅している場合はNoneを返す。
    実行中はsniper本体が現れるまでウォームアップ的に小さい値が続くため、収束検知
    ループ側の_WARMUP_SKIP/_TOLERANCEで自然に弾かれる想定。
    """
    try:
        out = subprocess.run(
            ["ssh", *ssh_opts, ssh_host,
             f"[ -f {pgid_file} ] && ps -o pcpu= -g $(cat {pgid_file}) 2>/dev/null"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None
        lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return None  # プロセスグループ消滅 = ジョブ完了/失敗
        return sum(float(l) for l in lines)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _converge_and_record(sample_fn, workload: str, bench_class: str,
                          num_threads: int, machine: str) -> None:
    """
    sample_fn()を収束検知方式でポーリングし、収束したら width_profile.json へ記録する
    共通ループ。sample_fnは引数無しでCPU%(float)またはNone(消滅/未観測)を返す
    呼び出し可能オブジェクトであること。

    ジョブが収束前に終了した場合(SizeSの短時間ジョブ等)は記録しない
    ―実行時間全体がウォームアップ区間に収まり、真の定常値を観測できていない
    可能性が高いため(project_scheduling_model参照)。
    """
    samples: list[float] = []
    start = time.time()
    while time.time() - start < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        pct = sample_fn()
        if pct is None:
            return  # プロセス消滅 = 収束前に完了、または対象フェーズ未到達。記録しない。
        samples.append(pct)
        if len(samples) <= _WARMUP_SKIP:
            continue
        window = samples[-_WINDOW:]
        if len(window) < _WINDOW:
            continue
        mean = sum(window) / len(window)
        if mean <= 0:
            continue
        spread = (max(window) - min(window)) / mean
        if spread <= _TOLERANCE:
            key = _key(workload, bench_class, num_threads, machine)
            with _lock:
                data = _load()
                data[key] = {
                    "workload":    workload,
                    "bench_class": bench_class,
                    "num_threads": num_threads,
                    "machine":     machine,
                    "width_pct":   round(mean, 1),
                    "samples":     [round(w, 1) for w in window],
                    "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save(data)
            return
    # _MAX_WAITまでに収束しなかった場合は記録しない(BTのように並列度次第で
    # 突発的に不安定化するワークロードの可能性があり、中途半端な値を残さない方が安全)。


def probe_and_record(container_name: str, workload: str, bench_class: str,
                      num_threads: int, machine: str = "sid") -> None:
    """SID(podman)版。ブロッキング関数なので、呼び出し側は別スレッド(daemon=True)で
    起動すること。"""
    _converge_and_record(lambda: _sample_cpu_pct(container_name),
                          workload, bench_class, num_threads, machine)


def probe_and_record_remote(pgid_file: str, ssh_host: str, ssh_opts: list[str],
                             workload: str, bench_class: str, num_threads: int,
                             machine: str) -> None:
    """Purple(SSH経由)版。ブロッキング関数なので、呼び出し側は別スレッド
    (daemon=True)で起動すること。"""
    _converge_and_record(lambda: _sample_cpu_pct_remote(pgid_file, ssh_host, ssh_opts),
                          workload, bench_class, num_threads, machine)


def get_measured_width(workload: str, bench_class: str, num_threads: int,
                        machine: str = "sid") -> float | None:
    """
    記録済みの実測width%を返す。優先順位:
      1. 完全一致 (workload, bench_class, num_threads, machine)
      2. 同workload+machine、同num_threads、別bench_class
         (project_scheduling_model: 「定常状態の実消費コアは問題サイズに依存せず
         スレッド数のみで決まる」という実測知見に基づき、クラスをまたいだ流用を
         スレッド数外挿より優先する)
      3. 同workload+machine、別num_threads(最も近いスレッド数、bench_class問わず。
         スレッド数方向のスケーリングは不確実なため係数を掛けず、最近傍の実測値を
         そのまま流用する=安全側)
    該当なしならNone。
    """
    with _lock:
        data = _load()

    exact_key = _key(workload, bench_class, num_threads, machine)
    if exact_key in data:
        return data[exact_key]["width_pct"]

    candidates = [
        (entry["bench_class"], entry["num_threads"], entry["width_pct"])
        for entry in data.values()
        if entry["workload"] == workload and entry["machine"] == machine
    ]

    if not candidates:
        return None

    same_th = [c for c in candidates if c[1] == num_threads]
    if same_th:
        cls, th, pct = same_th[0]
        print(f"[width_profile] {exact_key}: 実測なし → 同スレッド数の別クラス"
              f"({workload}_{cls}_{th})の実測{pct}%を流用")
        return pct

    nearest = min(candidates, key=lambda c: abs(c[1] - num_threads))
    cls, th, pct = nearest
    print(f"[width_profile] {exact_key}: 実測なし → 最近傍スレッド数"
          f"({workload}_{cls}_{th})の実測{pct}%を流用")
    return pct
