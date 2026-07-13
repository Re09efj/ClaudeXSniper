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
#
# 2026-07-11: 上限値を「物理/論理コア数ちょうど」から一段引き下げた。BT_W_16TH×3並列
# (ultra_orchestrator経由、CPU%モデル上は容量・ハード上限とも範囲内と正しく判定)が
# 22:54/22:56/22:59とほぼ3分間隔で投入された後、load averageが(1分値の移動平均で
# あるがゆえの反応遅れにより)投入判定が終わった後になってから31まで上昇し、
# 旧上限24を実際に超過する事故が発生した(emergency_stop.shで手動収束)。
# Purple側も同時期に59〜65まで上昇し、旧上限56を超過した。
# 「コア数ちょうど」という理論値は、投入判定の瞬間だけを見る限りは正しいが、
# 判定後に既に投入済みのジョブの負荷が遅れて乗ってくる分の余白が無かったため、
# 実務上は不十分と判明(2026-07-10の52超過事故より抑えられてはいるが、再発した)。
# 実測オーバーシュート幅(SID: 24→31で+7、Purple: 56→59-65で+3〜9)を踏まえ、
# 理論上限からマージンを差し引いた値に変更する。
SID_LOADAVG_HARD_LIMIT    = 20.0  # SID物理コア数24からマージン4を差し引き
PURPLE_LOADAVG_HARD_LIMIT = 48.0  # Purple論理コア数56からマージン8を差し引き

# 実測壁時計時間(W級・16TH、Data/run_profile.json)ベースの重量級判定。
#
# 2026-07-11: 旧定義{canneal, BT, dedup}を実測で再検証したところ、dedupが
# 実際には最軽量クラス(453.7s、IS(284.7s)に次いで軽い)であることが判明。
# 旧コメントの「dedup(3506s)」という記録は、dedupのスレッド数制約(3n+3、
# 有効値6/9/12/15)が判明する前の16THそのままの実行値だった可能性が高く、
# 現在の(resolve_valid_num_threadsで15THに丸めた)実行条件とは別物だったと
# 推測される。実測W_16(SID)の順位は BT(9908.2s) > canneal(4180.0s) >>
# GUPS(1607.9s) ≈ MG(1580.6s) > CG(674.5s) > FT(609.8s) > dedup(453.7s) >
# IS(284.7s)、LUはW級データ皆無(S_2=83.0sのみ)。この実測を踏まえ、
# ユーザー判断でBT/canneal/GUPSの上位3つを重量級とした(dedupは除外、
# GUPSはメモリ帯域律速でMEMORY_BOUND_WORKLOADSにも該当するため元々重い
# 部類という直感とも整合する)。
#
# 2026-07-09: 元は akarin/generate_candidates.py 単体でAKARIN候補alpha点数の
# 粗密判定にのみ使っていたが(2026-07-11にalpha自体を廃止しこの用途は消滅、
# akarin/README.md参照)、ultra_orchestrator.py のマシン振り分け(SID/Purple)
# も全く同じ「壁時計時間で重いワークロードはどれか」という同一概念を指す実測に
# 基づいていた(元run_tonight.pyの_WEIGHT_ORDER)ため、別々に定義すると2026-07-06に
# 実際に起きた定数ズレバグ(NPB判定とAKARIN粗密判定が同じ定数を誤って共有し、
# 片方の変更でもう片方が壊れた)の再発リスクがあった。同じ概念は1箇所で定義する
# 方針に統一し、こちらを正本とした(現在はマシン振り分け専用)。
#
# MEMORY_BOUND_WORKLOADS(下記)とは全くの別概念(こちらは壁時計時間の重さ、
# 下はメモリ帯域律速かどうか)である点に注意。たまたまGUPS/canneal/FT/CGが
# 両方に含まれることがあっても、意味的には無関係。
#
# 2026-07-12: LUを追加。2026-07-11時点では「LUはW級データ皆無(S_2=83.0sのみ)」
# として判断保留だったが、その後のSizeW本番実行(logs/sizeW_resend_20260712_005019.log)
# の[profile/estimate]でPurple側の推定壁時計時間が判明し、CG/dedupを大きく上回る
# ことが分かった(LU: 2TH 8946s/8TH 6501s/12TH 6501s/16TH 6936s、CGの最大5934s・
# dedupの最大3720sをいずれも上回る)。Purple側のワークロード中で最も重いと判断し、
# SID側へ振り分ける。
HEAVY_WORKLOADS = {"BT", "canneal", "GUPS", "LU"}

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
    # dedup: 2026-07-11に発覚した不整合を修正。旧値(2/8/12/16キー)は2026-07-06、
    # dedupの実スレッド数が`-t N`に対して3N+3になる問題(2026-07-08発見)を修正する
    # 前の測定で、「-t 2/8/12/16」=実スレッド数9/27/39/51というテスト条件だった。
    # 現行パイプラインはSniperのシミュレートコア数(=欲しい実スレッド数)を
    # resolve_valid_num_threads()で6/9/12/15に丸めてから逆算した-t値を渡すため、
    # 全く別の実行条件のCPU%を借用していた(6TH/9TH/15THは特に無関係な値)。
    # 12THだけは偶然「-t 12」=実スレッド数12で当時の測定条件と一致するため、
    # 417.9%を正しい実測値として残す。
    # 6TH: 2026-07-11、再実行バッチ中に`podman stats`で実測(5並列、Packed/
    # Scatter/HPO/EPO/MPO、145.24〜151.08%、平均148.7%)。線形推定(208.9%)より
    # かなり低く、非単調な実態(12THでピーク)を裏付ける結果になった。
    # 9TH: 2026-07-11、同バッチ中に実測(2並列、MPO 187.53%/AKARIN 198.48%、
    # 平均193.0%)。6TH(148.7%)→9TH(193.0%)→12TH(417.9%)という緩やかな
    # 立ち上がりから急上昇に転じる形になり、「12THでピーク」の非単調カーブと
    # 整合する。15THは引き続き実測待ちの暫定値(12THから線形スケール、
    # 過小評価より過大評価の方が安全弁として無難という考え方)。
    ("dedup",   6): 148.7, ("dedup",   9): 193.0, ("dedup",  12): 417.9, ("dedup",  15): 522.4,
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
#  - barnes, UA, PR: 2026-07-14追加。8THの実測width_pct(Data/width_profile.json)は
#    BT 478.6%・LU 400.4%(計算律速クラスタ)とGUPS 268.0%・canneal 99.1%
#    (メモリ律速クラスタ)の中間で、この1点だけでは断定できなかったが、
#    アルゴリズム的にFT/IS/CGと同種の不規則メモリアクセスパターンを持つため
#    予防的に含める: barnesは八分木を辿るポインタチェイシング(Barnes-Hut)、
#    UAは非構造適応メッシュの間接アドレッシング(CGに近い疎アクセス)、
#    PRはグラフCSR構造を辿るgather/scatter(GAPBS系、BFS/SSSPと同族だが
#    futexデッドロックで早期除外され較正機会が無かった)。BTMZは同じNPB系の
#    新規追加でも密なブロック計算(BT/MGに近い構造化グリッド)のため対象外。
#    2/16THの実測が揃うまでは安全側に倒す判断(CG追加時と同じ方針)。
MEMORY_BOUND_WORKLOADS = {"GUPS", "canneal", "FT", "IS", "CG", "barnes", "UA", "PR"}
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


def _apply_width_multipliers(workload: str, pct: float) -> float:
    """メモリバウンド/BTの安全マージンを適用する。実測(width_profile)由来の値にも
    静的モデル由来の値にも同じく適用する必要がある(2026-07-12発見: 以前は実測が
    ヒットすると即returnしてこのマージンをすり抜けていたバグがあった)。マージンの
    根拠(CPU%が輻輳時に低く見える・BTは並列度次第で突発的に不安定化する)は
    「測定値そのものの誤差」ではなく「その値をそのまま並列度の予算にすると
    危険」という並行実行時の安全マージンなので、実測か静的モデルかに関わらず
    常に適用すべきもの。"""
    if workload in MEMORY_BOUND_WORKLOADS:
        pct *= MEMORY_BOUND_WIDTH_MULTIPLIER
    if workload == "BT":
        pct *= BT_WIDTH_MULTIPLIER
    return pct


# 2026-07-12: measured/baselineどちらの表にも無い、本当に何も分かっていない
# ワークロード(例: CG)向けの固定フォールバック値。以前は baseline=100 ×
# (num_threads/2)^0.413 という「根拠のない100%を起点にした式」で計算していたが、
# 実測値(BT 600%超、canneal 100%程度等)を踏まえると値の根拠が薄く、素直に
# 固定値にする方が誠実と判断。BT(実測600%超)を除く他ワークロード(canneal/MG/IS/
# FTは100〜130%程度)の水準とBTの外れ値の中間を取り、300に設定(2026-07-12、
# ユーザー判断: 600→400→300と段階的に調整、「未知＝BT級の最悪ケース」という
# 悲観を弱めた)。他の経路(実測/baseline既知)と同じく_apply_width_multipliersで
# メモリバウンド(×3)/BT(×2)のマージンが乗る。
_UNKNOWN_WORKLOAD_WIDTH_PCT = 300.0


def _static_width_pct(workload: str, num_threads: int) -> float:
    """SID実機の手動計測(2026-07-04〜07-11)に基づく静的モデル。マージン適用前の
    生の値を返す(呼び出し側で_apply_width_multipliersを適用すること)。"""
    if workload in {"canneal", "dedup", "x264", "GUPS"}:
        measured = {th: v for (wl, th), v in _WIDTH_MEASURED.items() if wl == workload}
        if num_threads in measured:
            return measured[num_threads]
        # 未測定のスレッド数は最も近い実測点で代用する(べき乗則外挿より安全側)
        nearest = min(measured, key=lambda th: abs(th - num_threads))
        return measured[nearest]
    if workload in _WIDTH_BASELINE_2TH:
        baseline = _WIDTH_BASELINE_2TH[workload]
        scale = (num_threads / 2) ** _WIDTH_EXPONENT
        return baseline * scale
    return _UNKNOWN_WORKLOAD_WIDTH_PCT


def host_width_pct(workload: str, num_threads: int, bench_class: str | None = None,
                    machine: str = "sid") -> float:
    """このワークロード・スレッド数がホストの実コアを何%消費するかの推定値。

    2026-07-12: bench_class/machineを指定すると、まずutility.width_profileの
    実測データ(収束検知方式でpodman statsから記録したもの)を優先して使う。
    無ければ従来通り静的モデル(_WIDTH_MEASURED/_WIDTH_BASELINE_2TH、SID実機ベース)
    にフォールバックする。bench_classを省略した既存呼び出しは従来通り静的モデルのみ。
    """
    if bench_class is not None:
        from utility.width_profile import get_measured_width
        measured_width = get_measured_width(workload, bench_class, num_threads, machine)
        if measured_width is not None:
            return _apply_width_multipliers(workload, measured_width)

    return _apply_width_multipliers(workload, _static_width_pct(workload, num_threads))


def purple_width_pct(workload: str, bench_class: str, num_threads: int) -> float:
    """
    Purple用。width_profileの実測があればそれを使う。無ければ生スレッド数への
    フォールバックではなく、SID側の値(host_width_pct、実測があればそれ、無ければ
    SID静的モデル)を代用する(2026-07-12、ユーザー判断)。

    根拠: width%(CPU使用率)は「並列度・輻輳特性」に強く依存する指標であり、
    シングルスレッド性能(クロック速度等、SID/Purple間の実行速度差の主因)が
    違っても大きくは変わらないだろう、という仮定。実際run_profile.jsonの
    壁時計時間比較では、Purpleは概してSIDより遅い(特にS級の短時間ジョブで
    顕著、2〜4.7倍)ものの、これは主にSSH経由の起動・転送オーバーヘッドや
    純粋な実行速度差であり、width%(=ホストのコアを何%消費するか)とは別軸の
    指標のため、SID実測をそのまま転用する方が生スレッド数(num_threads)を
    そのままwidthにするより実態に近いと判断。
    実測・SID代用のどちらの経路でもhost_width_pctと同じ安全マージンを適用する
    (実測時は明示的に、SID代用時はhost_width_pct内で適用済み)。
    """
    from utility.width_profile import get_measured_width
    measured_width = get_measured_width(workload, bench_class, num_threads, "purple")
    if measured_width is not None:
        return _apply_width_multipliers(workload, measured_width)
    return host_width_pct(workload, num_threads, bench_class=bench_class, machine="sid")


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
