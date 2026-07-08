"""
.gettsuushin.py
確定8ワークロードのうちx264を除く7ワークロード(BT/FT/IS/MG/canneal/dedup/GUPS)の
DeLoc通信行列(comm.csv/comm_size.csv/mem_access.csv)を、Purple上のdetloc-tracer
(Pin計装ツール、Sniperとは無関係)経由で取得し、Data/tsuushin/size{SIZE}/に集約する。

x264は2026-07-07の調査で対象から除外した: このPARSEC同梱バージョン(0.65.1047M)は
フレームを1枚エンコードするたびに新しいpthreadを使い捨てで生成するアーキテクチャ
のため、「--threadsで指定した固定N個のスレッドが通信する」という今回の前提
(cpu_mapへのN論理スレッド固定配置)とそもそも噛み合わない。二重解放クラッシュ
自体はcommon/set.cのx264_cqm_delete()を修正して解消したが、スレッド数問題は
設計上の非互換であり修正では直らないと判断。

dedupは実スレッド数が -t N に対して 3N+3 になる(パイプライン構造のため)ことが
判明しているが、そのまま使う方針。

1THは通信そのものが発生しないため対象外(2〜16THのみ)。

SizeW: BT/FT/IS/MG は本物のWクラスバイナリ(workload_binaries/npb_sizes/)を使用。
GUPSはテーブルサイズ指数を22→23に変更。canneal/dedupはWサイズの入力データが
存在しないため、静的にスケールした代替を用意した:
  - canneal: NSWAPS(反復回数)を10000→40000(4倍)
  - dedup: media.datを2つ連結したmedia_w.dat(2倍サイズ)を新規作成

BTは実行時間が極端に不安定(過去実測: SizeSの同一スレッド数でも12秒〜3442秒、
別バッチのSizeWでは最大6012秒/100分)なため、専用の長いタイムアウトを設ける。

使い方:
  python3 .gettsuushin.py
"""
import concurrent.futures
import glob
import os
import subprocess
import threading

SIZE = "W"

PURPLE_HOST   = "yuri@172.20.2.220"
LOCAL_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"size{SIZE}")
os.makedirs(LOCAL_OUT_DIR, exist_ok=True)

# 注意: これらは全てリモート側のシェルコマンド文字列に埋め込んで使う。
# ダブルクォートで囲むと "~/..." の ~ はbashで展開されない(リテラル扱いになる)ため、
# $HOME を使う。ただしscpのリモートパス引数は環境変数展開されない(2026-07-07に
# 実際に嵌った)ため、REMOTE_OUTだけは絶対パスを直書きする。
REMOTE_HOME   = "/home/gp.sc.cc.tohoku.ac.jp/yuri"
REMOTE_OUT    = f"{REMOTE_HOME}/deloc_test/comm_matrices_tsuushin_{SIZE}"

PIN  = "/home/agungm/pin-3.7-97619-g0d0c92f4f-gcc-linux/pin"
TOOL = f"{REMOTE_HOME}/deloc_test/obj-intel64/detloc_tracer.so"
NPB_W_BIN = f"{REMOTE_HOME}/deloc_test/workload_binaries/npb_sizes"
PARSEC    = f"{REMOTE_HOME}/claudex_akarin/binary/PARSEC"
GUPS_BIN  = f"{REMOTE_HOME}/claudex_akarin/binary/GUPS/gups"

THREAD_COUNTS = list(range(2, 17))  # 2〜16スレッド(1THは通信が発生しないため除外)
STANDARD_TIMEOUT = 700    # SizeSでの実測(107秒以内)より問題規模が大きいため余裕を見た値
BT_TIMEOUT       = 7200   # BTはSizeSで57分の実績あり、Wはさらに悪化しうるため2時間確保

# (workload, binary, args_template, timeout_sec)
# args_template は {th} をスレッド数で置換する
WORKLOADS = [
    ("BT",      f"{NPB_W_BIN}/bt.W.x", "", BT_TIMEOUT),
    ("FT",      f"{NPB_W_BIN}/ft.W.x", "", STANDARD_TIMEOUT),
    ("IS",      f"{NPB_W_BIN}/is.W.x", "", STANDARD_TIMEOUT),
    ("MG",      f"{NPB_W_BIN}/mg.W.x", "", STANDARD_TIMEOUT),
    ("canneal", f"{PARSEC}/pkgs/kernels/canneal/src/canneal",
     "{th} 40000 2000 " + f"{PARSEC}/pkgs/kernels/canneal/src/inputs/100000.nets 32",
     STANDARD_TIMEOUT),
    ("dedup", f"{PARSEC}/pkgs/kernels/dedup/src/dedup",
     "-c -p -v -t {th} -i " + f"{PARSEC}/pkgs/kernels/dedup/src/inputs/media_w.dat"
     + " -o /tmp/dedup_tsuushin_w_{th}.dat.ddp",
     STANDARD_TIMEOUT),
    ("GUPS", GUPS_BIN, "23", STANDARD_TIMEOUT),
]

MAX_PARALLEL = 10  # Purple上で同時に走らせるジョブ数(56論理コア中、無理のない範囲)

# BTは他ワークロードよりPin計装のオーバーヘッドが桁違いに大きく(命令数が多いため)、
# 2026-07-07にSizeWで8並列実行したところ通常CPU競合で軒並み遅延する事態が発生した。
# BTだけ同時実行数を3に絞る(GUPSのメモリ帯域競合対策とは別の、素朴なCPU競合対策)。
BT_CONCURRENCY = 3
_bt_semaphore = threading.Semaphore(BT_CONCURRENCY)


def already_done(workload: str, th: int) -> bool:
    pattern = os.path.join(LOCAL_OUT_DIR, f"{workload}_{SIZE}_{th}TH_*.comm.csv")
    return len(glob.glob(pattern)) > 0


def run_job(workload: str, binary: str, args_template: str, timeout_sec: int, th: int) -> str:
    prefix = f"{workload}_{SIZE}_{th}TH_"
    if already_done(workload, th):
        return f"[SKIP] {workload} {th}TH (既存)"

    sem = _bt_semaphore if workload == "BT" else None
    if sem is not None:
        sem.acquire()
    try:
        return _run_job_inner(workload, binary, args_template, timeout_sec, th, prefix)
    finally:
        if sem is not None:
            sem.release()


def _run_job_inner(workload: str, binary: str, args_template: str, timeout_sec: int,
                    th: int, prefix: str) -> str:
    args = args_template.format(th=th)
    remote_cmd = f"""
set -u
mkdir -p {REMOTE_OUT}
workdir=$(mktemp -d)
cd "$workdir"
export OMP_NUM_THREADS={th}
export GOMP_CPU_AFFINITY="$(seq -s' ' 0 {th - 1})"
timeout -k 20 {timeout_sec} {PIN} -t {TOOL} -s_prod_simple -- {binary} {args} > run.log 2>&1
for f in *.comm.csv *.comm_size.csv *.mem_access.csv; do
  [ -e "$f" ] && cp "$f" "{REMOTE_OUT}/{prefix}$(basename "$f")"
done
cd /
rm -rf "$workdir"
"""
    local_timeout = timeout_sec + 60
    try:
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", PURPLE_HOST, remote_cmd],
            timeout=local_timeout, capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return f"[LOCAL_TIMEOUT] {workload} {th}TH (sshラッパー自体が{local_timeout}秒を超過)"

    # comm.csv が実際に取れたかで成功判定する
    check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", PURPLE_HOST,
         f"ls {REMOTE_OUT}/{prefix}*.comm.csv 2>/dev/null"],
        capture_output=True, text=True,
    )
    if not check.stdout.strip():
        return f"[FAIL] {workload} {th}TH (comm.csv が生成されなかった)"

    scp_ret = subprocess.run(
        ["scp", "-q", f"{PURPLE_HOST}:{REMOTE_OUT}/{prefix}*", LOCAL_OUT_DIR + "/"],
    )
    if scp_ret.returncode != 0:
        return f"[SCP_FAIL] {workload} {th}TH"
    return f"[OK] {workload} {th}TH"


def main():
    # スレッド数を外側にすることで、BT(最も時間がかかる)のジョブが並列枠を
    # 先頭で独占して他ワークロードを長時間待たせるのを避ける。
    jobs = []
    for th in THREAD_COUNTS:
        for name, binary, args_template, timeout_sec in WORKLOADS:
            jobs.append((name, binary, args_template, timeout_sec, th))

    print(f"[gettsuushin] Size{SIZE}取得ジョブ数: {len(jobs)} (並列度={MAX_PARALLEL})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = [pool.submit(run_job, *j) for j in jobs]
        for fut in concurrent.futures.as_completed(futures):
            print(fut.result(), flush=True)

    print("[gettsuushin] 完了")


if __name__ == "__main__":
    main()
