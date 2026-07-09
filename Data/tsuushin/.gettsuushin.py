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

スレッド数の対象範囲はワークロードごとに個別指定する(2026-07-10、それまで全
ワークロード一律で1THを除外していたのを見直した)。「1THを除外する」目的は
出力の通信行列が1x1のスカラ(通信なし)に退化するのを避けるためであり、
起動引数の`-t 1`がそのまま実スレッド数1になるBT/FT/IS/MG/CG/LU/GUPSでは
妥当だが、canneal(`-t N`→実N+1)・dedup(`-t N`→実3N+3)は`-t 1`でも実スレッド数
2・6になり、ちゃんとした行列が得られるため1から含める(.gettsuushin_S.pyで
既に採用していた規約に合わせた)。当初この見直し前にcanneal_W_1TH・dedup_W_1TH
が欠損したまま撮り終えていたため、この修正を機に再実行して補完する。

SizeW: BT/FT/IS/MG は本物のWクラスバイナリ(workload_binaries/npb_sizes/)を使用。
GUPSはテーブルサイズ指数を22→23に変更。canneal/dedupはWサイズの入力データが
存在しないため、静的にスケールした代替を用意した:
  - canneal: NSWAPS(反復回数)を10000→40000(4倍)
  - dedup: media.datを2つ連結したmedia_w.dat(2倍サイズ)を新規作成

タイムアウトなし(2026-07-10撤去): comm.csv取得はSniperのbarrier同期を経由しない
Pin計装のみのネイティブ実行で、Sniper本体のfutexデッドロックとは無関係。時間が
かかっても待てばよい。

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
# CG/LU: npb_sizes/にはlu.W.xが存在しないため、S側と同じclaudex_akarinミラーに統一
# (cg.W.xはnpb_sizes/にも存在するが、CG/LUを同じパス規約に揃えるためこちらを使う)。
NPB_BIN = f"{REMOTE_HOME}/claudex_akarin/binary/NPB3.3-OMP/bin"

_FULL      = list(range(2, 17))   # -t N = 実N のワークロード用(1THはスカラ行列になるため除外)
_FROM_ONE  = list(range(1, 17))   # canneal/dedup用(-t 1でも実スレッド数>1になるため含める)

# (workload, binary, args_template, arg_thread_values)
# args_template は {th} をスレッド数で置換する
WORKLOADS = [
    ("BT",      f"{NPB_W_BIN}/bt.W.x", "", _FULL),
    ("FT",      f"{NPB_W_BIN}/ft.W.x", "", _FULL),
    ("IS",      f"{NPB_W_BIN}/is.W.x", "", _FULL),
    ("MG",      f"{NPB_W_BIN}/mg.W.x", "", _FULL),
    ("canneal", f"{PARSEC}/pkgs/kernels/canneal/src/canneal",
     "{th} 40000 2000 " + f"{PARSEC}/pkgs/kernels/canneal/src/inputs/100000.nets 32",
     _FROM_ONE),
    ("dedup", f"{PARSEC}/pkgs/kernels/dedup/src/dedup",
     "-c -p -v -t {th} -i " + f"{PARSEC}/pkgs/kernels/dedup/src/inputs/media_w.dat"
     + " -o /tmp/dedup_tsuushin_w_{th}.dat.ddp",
     _FROM_ONE),
    ("GUPS", GUPS_BIN, "23", _FULL),
    ("CG", f"{NPB_BIN}/cg.W.x", "", _FULL),
    ("LU", f"{NPB_BIN}/lu.W.x", "", _FULL),
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


def run_job(workload: str, binary: str, args_template: str, th: int) -> str:
    prefix = f"{workload}_{SIZE}_{th}TH_"
    if already_done(workload, th):
        return f"[SKIP] {workload} {th}TH (既存)"

    sem = _bt_semaphore if workload == "BT" else None
    if sem is not None:
        sem.acquire()
    try:
        return _run_job_inner(workload, binary, args_template, th, prefix)
    finally:
        if sem is not None:
            sem.release()


def _run_job_inner(workload: str, binary: str, args_template: str, th: int, prefix: str) -> str:
    args = args_template.format(th=th)
    remote_cmd = f"""
set -u
mkdir -p {REMOTE_OUT}
workdir=$(mktemp -d)
cd "$workdir"
export OMP_NUM_THREADS={th}
export GOMP_CPU_AFFINITY="$(seq -s' ' 0 {th - 1})"
{PIN} -t {TOOL} -s_prod_simple -- {binary} {args} > run.log 2>&1
for f in *.comm.csv *.comm_size.csv *.mem_access.csv; do
  [ -e "$f" ] && cp "$f" "{REMOTE_OUT}/{prefix}$(basename "$f")"
done
cd /
rm -rf "$workdir"
"""
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", PURPLE_HOST, remote_cmd],
        capture_output=True,
    )

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
    for th in range(1, 17):
        for name, binary, args_template, arg_values in WORKLOADS:
            if th in arg_values:
                jobs.append((name, binary, args_template, th))

    print(f"[gettsuushin] Size{SIZE}取得ジョブ数: {len(jobs)} (並列度={MAX_PARALLEL})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = [pool.submit(run_job, *j) for j in jobs]
        for fut in concurrent.futures.as_completed(futures):
            print(fut.result(), flush=True)

    print("[gettsuushin] 完了")


if __name__ == "__main__":
    main()
