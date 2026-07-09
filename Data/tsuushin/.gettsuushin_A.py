"""
.gettsuushin_A.py
SizeA用の通信行列取得。Purple上のdetloc-tracer経由で、確定9ワークロード
(BT/FT/IS/MG/CG/LU/canneal/dedup/GUPS)全部を対象にする。

2026-07-10時点で存在した旧sizeA(BT/FT/IS/MG の 2/6/8/12THのみ、1刻み規約以前の
部分データ)は全削除してこのスクリプトで撮り直す方針。

canneal(netlist=200000.nets)・dedup(media_a.dat)のA-class入力ファイルはPurple側の
claudex_akarinミラーに存在しなかったため、このスクリプト作成時にローカルから
scpでコピー済み(ファイルサイズ一致確認済み)。

CG/LUはS/W取得時と同じ理由(deloc_test/workload_binaries/npb_sizes/にlu.*.xが
存在しない)でclaudex_akarin/binary/NPB3.3-OMP/bin/のミラーを使う。BT/FT/IS/MGは
npb_sizes/にA-classバイナリが存在するためそちらを使う(W-classと同じ規約)。

タイムアウトなし: comm.csv取得はSniperのbarrier同期を経由しないPin計装のみの
ネイティブ実行で、Sniper本体のfutexデッドロック(project_sniper_futex_deadlock
メモリ参照)とは無関係。時間がかかっても待てばよいので、timeout/ssh呼び出しの
タイムアウトを一切設けない(2026-07-10、以前のS/W取得スクリプトにあった
timeout_sec/local_timeoutは撤去)。

スレッド数の対象範囲はワークロードごとに個別指定する(2026-07-10、それまで全
ワークロード一律で1THを除外していたのを見直した)。「1THを除外する」目的は
出力の通信行列が1x1のスカラ(通信なし)に退化するのを避けるためであり、
起動引数の`-t 1`がそのまま実スレッド数1になるBT/FT/IS/MG/CG/LU/GUPSでは
妥当だが、canneal(`-t N`→実N+1)・dedup(`-t N`→実3N+3)は`-t 1`でも実スレッド数
2・6になり、ちゃんとした行列が得られるため1から含める(.gettsuushin_S.pyで
既に採用していた規約をW/Aにも合わせて明示化)。

使い方:
  python3 .gettsuushin_A.py
"""
import concurrent.futures
import glob
import os
import subprocess
import threading

SIZE = "A"

PURPLE_HOST   = "yuri@172.20.2.220"
LOCAL_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"size{SIZE}")
os.makedirs(LOCAL_OUT_DIR, exist_ok=True)

REMOTE_HOME   = "/home/gp.sc.cc.tohoku.ac.jp/yuri"
REMOTE_OUT    = f"{REMOTE_HOME}/deloc_test/comm_matrices_tsuushin_{SIZE}"

PIN  = "/home/agungm/pin-3.7-97619-g0d0c92f4f-gcc-linux/pin"
TOOL = f"{REMOTE_HOME}/deloc_test/obj-intel64/detloc_tracer.so"
NPB_A_BIN = f"{REMOTE_HOME}/deloc_test/workload_binaries/npb_sizes"
NPB_BIN   = f"{REMOTE_HOME}/claudex_akarin/binary/NPB3.3-OMP/bin"   # CG/LU用(npb_sizes/にlu.*.xが無いため)
PARSEC    = f"{REMOTE_HOME}/claudex_akarin/binary/PARSEC"
GUPS_BIN  = f"{REMOTE_HOME}/claudex_akarin/binary/GUPS/gups"

_FULL      = list(range(2, 17))   # -t N = 実N のワークロード用(1THはスカラ行列になるため除外)
_FROM_ONE  = list(range(1, 17))   # canneal/dedup用(-t 1でも実スレッド数>1になるため含める)

# (workload, binary, args_template, arg_thread_values)
WORKLOADS = [
    ("BT",      f"{NPB_A_BIN}/bt.A.x", "", _FULL),
    ("FT",      f"{NPB_A_BIN}/ft.A.x", "", _FULL),
    ("IS",      f"{NPB_A_BIN}/is.A.x", "", _FULL),
    ("MG",      f"{NPB_A_BIN}/mg.A.x", "", _FULL),
    ("CG",      f"{NPB_BIN}/cg.A.x", "", _FULL),
    ("LU",      f"{NPB_BIN}/lu.A.x", "", _FULL),
    ("canneal", f"{PARSEC}/pkgs/kernels/canneal/src/canneal",
     "{th} 15000 2000 " + f"{PARSEC}/pkgs/kernels/canneal/src/inputs/200000.nets 64",
     _FROM_ONE),
    ("dedup", f"{PARSEC}/pkgs/kernels/dedup/src/dedup",
     "-c -p -v -t {th} -i " + f"{PARSEC}/pkgs/kernels/dedup/src/inputs/media_a.dat"
     + " -o /tmp/dedup_tsuushin_a_{th}.dat.ddp",
     _FROM_ONE),
    ("GUPS", GUPS_BIN, "24", _FULL),
]

MAX_PARALLEL = 6  # SizeWの10より落とす(1ジョブあたりの負荷が重いため)

BT_CONCURRENCY = 2  # SizeWの3よりさらに絞る(BTのPin計装オーバーヘッド対策)
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
    # 先頭で独占して他ワークロードを長時間待たせるのを避ける
    # (.gettsuushin.py/SizeWと同じ対策。2026-07-10、ワークロード外側で組んだ結果
    # BT_CONCURRENCY=2があってもMAX_PARALLEL=6の枠が最初の6件=BT×6で埋まり
    # 他ワークロードが全く始まらない事故が発生したため修正)。
    jobs = []
    for th in range(1, 17):
        for name, binary, args_template, arg_values in WORKLOADS:
            if th in arg_values:
                jobs.append((name, binary, args_template, th))

    print(f"[gettsuushin_A] Size{SIZE}取得ジョブ数: {len(jobs)} (並列度={MAX_PARALLEL})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = [pool.submit(run_job, *j) for j in jobs]
        for fut in concurrent.futures.as_completed(futures):
            print(fut.result(), flush=True)

    print("[gettsuushin_A] 完了")


if __name__ == "__main__":
    main()
