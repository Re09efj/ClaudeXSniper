"""
capacity_model.py
ワークロード・スレッド数から「ホストの実効コアをどれだけ消費するか(width)」
「壁時計時間がどれだけかかるか(duration)」を見積もるコストモデル。
ultra_orchestrator.py から2026-07-09に切り出した(スケジューリングの意思決定は
一切含まない、純粋な推定ロジックのため)。

project_scheduling_model メモリ参照:
  - duration (壁時計時間): utility.run_profile の実測値/推定値
  - width (実消費ホストコア数%): ワークロード種別ごとの2THベースライン ×
    スレッド数スケーリング (cost(threads) ≈ baseline × (threads/2)^0.413)
    ワークロードの実行時間そのもの(研究上の性能比較)とは無関係な、
    Sniper自体のホストCPU消費効率の話である点に注意。
"""

import os
import subprocess

from utility.run_profile import get_reference, estimate_walltime
from utility.sniper_sim_purple import SSH_HOST as PURPLE_SSH_HOST

SID_CAPACITY_DEFAULT    = 21.0  # hiragahama(hostname: sid) 実効コア数上限
PURPLE_CAPACITY_DEFAULT = 45.0  # Purpleは56論理コアの共有サーバ、生スレッド数ベースで上限45(2026-07-05: 50→45に削減)

# 2026-07-10のスケジューリング事故(load average 52超過、CG/LU未較正ワークロード)を
# 受けて追加。live_*_load_cores()(podman stats/ps由来のCPU使用率)は「実際に消費した
# CPUサイクル」しか見ておらず、メモリ帯域待ちでブロックされているスレッド(D-state/
# 実行待ち)を検知できない。これは2026-07-06のGUPS実効24〜27%効率の発覚時と全く
# 同じ盲点で、その時はGUPS個別にMEMORY_BOUND_WIDTH_MULTIPLIERを追加しただけで、
# 当日追加されたばかりの未較正ワークロード(CG/LU)がすり抜けて再発した。
# load average(実行待ちキューの長さ)はCPU%が拾えないこの種の輻輳を直接検知できる
# ため、静的モデル(host_width_pct)未較正のワークロードに対する汎用的な最終安全弁
# として、既存のCPU%ゲートに加えて導入する。
# 上限値は物理/論理コア数ちょうど(SID_HARD_LIMIT_CORES/PURPLE_HARD_LIMIT_CORESと同じ
# 基準)。load average > コア数は定義上「今まさに実行待ち/ブロック中のスレッドが
# コア数を超えている」=既に飽和している状態なので、そこに上乗せの余裕を持たせる
# 理由がない(むしろCPU%側より広く輻輳を拾う指標である以上、緩めるのは筋が悪い)。
SID_LOADAVG_HARD_LIMIT    = 24.0  # SID物理コア数ちょうど
PURPLE_LOADAVG_HARD_LIMIT = 56.0  # Purple論理コア数ちょうど

# 実測壁時計時間(W級、Data/run_profile.json)ベースの重量級判定。当初はNPB系を
# 一律「重量級」とみなしていたが、2026-07-06の実測で canneal(10184s) > BT(10036s)
# > x264(7765s) > dedup(3506s) > MG(1715s) > GUPS(1534s) > FT(829s) > IS(401s)と
# 判明し、「NPBかどうか」ではなく実測に基づき上位3つを重量級とする方針に変更。
# 2026-07-07にx264をワークロード自体から対象外にしたため、上位3つがcanneal/BT/dedup
# に繰り上がった(x264の代わりにdedupが3位)。
#
# 2026-07-09: 元は akarin/generate_candidates.py 単体でAKARIN候補alpha点数の
# 粗密判定にのみ使っていたが、ultra_orchestrator.py のマシン振り分け(SID/Purple)
# も全く同じ「壁時計時間で重いワークロードはどれか」という同一概念を指す実測に
# 基づいていた(元run_tonight.pyの_WEIGHT_ORDER)ため、別々に定義すると2026-07-06に
# 実際に起きた定数ズレバグ(NPB判定とAKARIN粗密判定が同じ定数を誤って共有し、
# 片方の変更でもう片方が壊れた)の再発リスクがあった。同じ概念は1箇所で定義する
# 方針に統一し、こちらを正本とした。
#
# MEMORY_BOUND_WORKLOADS(下記)とは全くの別概念(こちらは壁時計時間の重さ、
# 下はメモリ帯域律速かどうか)である点に注意。たまたまcanneal/FTが両方に
# 含まれることがあっても、意味的には無関係。
HEAVY_WORKLOADS = {"canneal", "BT", "dedup"}

# 2026-07-06のスケジューリング事故(load average 55超過)を受けて追加した
# ハード上限。_WIDTH_BASELINE_2TH未収載のワークロードはhost_width_pct()の
# デフォルト(100%)にフォールバックするため、静的モデルだけでは実際の消費を
# 過小評価しうる。live_*_load_cores() で実測した「今まさに使われているコア数」
# を踏まえ、これを超える場合はジョブ投入を遅らせるゲートを_CapacityPool(utility.scheduling)
# に追加した。静的モデル(LPT/CP-SAT)によるジョブの「順序付け」は従来通り使い、
# この実測ゲートは投入の可否だけを最終チェックする安全弁という位置づけ(ハイブリッド方式)。
SID_HARD_LIMIT_CORES    = 24.0  # SIDの物理コア数そのもの
PURPLE_HARD_LIMIT_CORES = 56.0  # Purpleの論理コア数そのもの

# ワークロード種別ごとの実消費ホストコア(%) @ 2TH実測 (project_scheduling_model参照)
# BT/FT/IS/MG(NPB)はBTの実測4点フィットから得たべき乗則 cost(threads)≈baseline×(threads/2)^0.413
# で外挿する。GAPBS系(BFS/PR/TC/BC/CC/SSSP)は2026-07-06にクラッシュ再発のため
# ワークロード自体を削除。
_WIDTH_BASELINE_2TH = {
    "BT": 133, "FT": 117, "IS": 102, "MG": 100,
}
_WIDTH_EXPONENT = 0.413  # BTの実測4点フィット cost(threads)≈99.6×threads^0.413 の指数部を流用

# canneal/dedup/x264/GUPSは2026-07-06に2/8/12/16THを実測したところ、BTのような
# べき乗則スケーリングに従わない(cannealはほぼ横ばい、dedupは12THでピークになる
# 非単調な挙動)ことが判明したため、_WIDTH_EXPONENTでの外挿ではなく実測値を
# 直接引く方式にした。未測定の組み合わせ(bench_class違い等)は同ワークロードの
# 最も近いスレッド数の実測値にフォールバックする。
_WIDTH_MEASURED = {
    ("canneal", 2): 95.0, ("canneal", 8): 94.9, ("canneal", 12): 92.0, ("canneal", 16): 94.6,
    ("dedup",   2): 129.0, ("dedup",   8): 378.7, ("dedup",   12): 417.9, ("dedup",   16): 387.7,
    ("x264",    2): 93.0, ("x264",    8): 209.7, ("x264",    12): 215.1, ("x264",    16): 203.8,
    ("GUPS",    2): 147.0, ("GUPS",    8): 189.5, ("GUPS",    12): 206.1, ("GUPS",    16): 227.6,
}


# メモリ帯域律速のワークロードは、podman stats実測のCPU使用率(_WIDTH_MEASURED)
# だけでは同時実行数を絞る根拠にならない(ホストCPU消費が低く見えても、実際は
# メモリ帯域を奪い合って渋滞する)。2026-07-07: SIDでGUPS/16THを9並列実行した際に
# 各ジョブが実効24〜27%のCPUしか進まない自己渋滞が発生したことで判明。
# 判定基準:
#  - GUPS, canneal: _WIDTH_MEASURED実測がスレッド数によらず低いまま横ばい
#    (GUPS 147→227%、canneal 92〜95%)= ホストがメモリ待ちで遊んでいる兆候。
#    特にcannealはPARSEC中で最もキャッシュ効率が悪いポインタチェイシング系として
#    文献でも知られる。逆にdedup(129→417%)・x264(93→215%)はスレッド数に応じて
#    実測値が伸びており計算律速と判断、対象外。
#  - FT, IS: 実測データなし(BT/FT/IS/MGはべき乗則モデル側)だが、NPBの中でも
#    FT(大ストライドFFTバタフライ)・IS(大規模scatter/gatherバケツソート)は
#    アルゴリズム的にメモリ帯域律速として知られるため予防的に含める。
#    BT/MGは構造化グリッド計算でキャッシュ再利用が効きやすいため対象外。
#  - CG: 2026-07-10追加。疎行列(不規則スパースアクセス、ポインタチェイシングに
#    近いgather/scatterパターン)でFT/ISと同種のメモリ帯域律速として文献でも
#    知られるため予防的に含める(未較正のまま同日にスケジューリング事故の一因と
#    なったworkloadなので、実測が揃うまでは安全側に倒す)。LUはブロック化された
#    構造的アクセス(BT/SPに近い波面並列)のため対象外のまま。
MEMORY_BOUND_WORKLOADS = {"GUPS", "canneal", "FT", "IS", "CG"}
# 2026-07-09: SizeS本番実行でcanneal(memory-bound)のAKARINジョブが並列輻輳で
# 6件タイムアウトしたため、2.0→3.0に引き上げ(同時実行数をさらに絞る)。
MEMORY_BOUND_WIDTH_MULTIPLIER = 3.0

# BTは実行時間が並列度に応じて極端に不安定(Pin計装下で12秒〜3442秒、2026-07-07)。
# メモリ帯域律速(GUPS/canneal型、並列度に応じて連続的に悪化)とは違う挙動
# (単発なら速いが並列が重なると突発的に遅くなる)で、原因はCPU競合に敏感な
# 何らかの特性と推定されるが未特定。2026-07-09にSizeS本番実行でも1件タイムアウト
# ・単発リトライでは102秒(タイムアウト枠600秒の1/6)で完走という同型の挙動を
# 再確認し、一度きりの偶発事象ではないとユーザーが判断。原因の理屈は
# MEMORY_BOUND_WORKLOADSと異なるが、対策(同時実行数を絞る)は同じ仕組みで
# 実現できるため、BT専用の実消費コア倍率を別途設ける。
BT_WIDTH_MULTIPLIER = 2.0


def host_width_pct(workload: str, num_threads: int) -> float:
    """このワークロード・スレッド数がホストの実コアを何%消費するかの推定値。"""
    if workload in {"canneal", "dedup", "x264", "GUPS"}:
        measured = {th: v for (wl, th), v in _WIDTH_MEASURED.items() if wl == workload}
        if num_threads in measured:
            pct = measured[num_threads]
        else:
            # 未測定のスレッド数は最も近い実測点で代用する(べき乗則外挿より安全側)
            nearest = min(measured, key=lambda th: abs(th - num_threads))
            pct = measured[nearest]
    else:
        baseline = _WIDTH_BASELINE_2TH.get(workload, 100)
        scale = (num_threads / 2) ** _WIDTH_EXPONENT
        pct = baseline * scale

    if workload in MEMORY_BOUND_WORKLOADS:
        pct *= MEMORY_BOUND_WIDTH_MULTIPLIER
    if workload == "BT":
        pct *= BT_WIDTH_MULTIPLIER
    return pct


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


def live_sid_loadavg() -> float:
    """
    SIDホストのload average(1分値、os.getloadavg())。podman stats(CPU%)と違い、
    メモリ帯域待ちでブロックされているスレッドも実行待ちキューとしてカウントされるため、
    CPU%ゲートが見逃す種類の輻輳(2026-07-06/07-10のインシデント)を検知できる。
    """
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def live_purple_loadavg() -> float:
    """Purpleホストのload average(1分値)。SSH経由で/proc/loadavgを取得する。"""
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
             PURPLE_SSH_HOST, "cat /proc/loadavg"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return float(out.split()[0])
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
