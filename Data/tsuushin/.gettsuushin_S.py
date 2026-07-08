"""
.gettsuushin_S.py
SizeS用の通信行列取得。.gettsuushin.py(SizeW)と同じPurple上のdetloc-tracer経由だが、
対象はcanneal/dedupの2つだけに絞る(BT/FT/IS/MG/GUPSは初期データ取得時に
Sクラスの通信行列を既にData/comm_matrices/に保有済みのため不要)。

canneal/dedupはSクラス側でも「起動時の-t N」と「実スレッド数」が一致しない
(canneal: 実=N+1、dedup: 実=3N+3。utility/cpu_affinity.pyのarg_threads_for参照)。
2026-07-08にJob.num_threadsの意味を「常に実スレッド数」に統一したため、cpu_map・
MPO計算はこの実スレッド数を要求する。

当初はSniper側(標準THREAD_COUNTS=[2,8,12,16]相当)に必要な4点(canneal: -t 1,7,11,15
/ dedup: -t 1,2,3,4)だけを集めていたが、これとは別に機械学習(NetLSD+二段階k-NN、
[[project_lsd_knn_experiments]]参照)側で「1刻みの細粒度通信行列」が必要という
要求があり、SizeW(.gettsuushin.py)で既にBT/FT/IS/MG/GUPS/canneal/dedupの2〜16全刻みを
取得済みなのと同じ粒度をSizeSにも揃えることにした(2026-07-08)。よって対象は
`-t 1`〜`16`の全16点に拡張。already_done()が既存の4点をスキップするので、
残り12点×2ワークロード=24点が新規に追加される。

.gettsuushin.py(SizeW)は「1THは通信が発生しないため対象外」として2始まりだったが、
これはNPB/GUPSのような「-t N = 実N」のワークロードの話であり、canneal(-t1→実2
スレッド)・dedup(-t1→実6スレッド)には当てはまらない。よってこちらは-t 1から含める。

使い方:
  python3 .gettsuushin_S.py
"""
import concurrent.futures
import glob
import os
import subprocess

SIZE = "S"

PURPLE_HOST   = "yuri@172.20.2.220"
LOCAL_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"size{SIZE}")
os.makedirs(LOCAL_OUT_DIR, exist_ok=True)

REMOTE_HOME   = "/home/gp.sc.cc.tohoku.ac.jp/yuri"
REMOTE_OUT    = f"{REMOTE_HOME}/deloc_test/comm_matrices_tsuushin_{SIZE}"

PIN  = "/home/agungm/pin-3.7-97619-g0d0c92f4f-gcc-linux/pin"
TOOL = f"{REMOTE_HOME}/deloc_test/obj-intel64/detloc_tracer.so"
PARSEC = f"{REMOTE_HOME}/claudex_akarin/binary/PARSEC"

STANDARD_TIMEOUT = 700

# (workload, binary, args_template(-t Nに相当する起動引数), timeout_sec, arg_thread_values)
# args_templateの{th}は起動引数の値(実スレッド数ではなく-t Nそのもの)で置換する。
# Sクラスのパラメータはutility/cpu_affinity.pyのCANNEAL_PARAMS["S"]/DEDUP_INPUT_BY_CLASS["S"]
# と揃えている(NSWAPS=10000/100000.nets、media.dat)。
WORKLOADS = [
    ("canneal", f"{PARSEC}/pkgs/kernels/canneal/src/canneal",
     "{th} 10000 2000 " + f"{PARSEC}/pkgs/kernels/canneal/src/inputs/100000.nets 32",
     STANDARD_TIMEOUT, list(range(1, 17))),
    ("dedup", f"{PARSEC}/pkgs/kernels/dedup/src/dedup",
     "-c -p -v -t {th} -i " + f"{PARSEC}/pkgs/kernels/dedup/src/inputs/media.dat"
     + " -o /tmp/dedup_tsuushin_s_{th}.dat.ddp",
     STANDARD_TIMEOUT, list(range(1, 17))),
]

MAX_PARALLEL = 8


def already_done(workload: str, th: int) -> bool:
    pattern = os.path.join(LOCAL_OUT_DIR, f"{workload}_{SIZE}_{th}TH_*.comm.csv")
    return len(glob.glob(pattern)) > 0


def run_job(workload: str, binary: str, args_template: str, timeout_sec: int, th: int) -> str:
    prefix = f"{workload}_{SIZE}_{th}TH_"
    if already_done(workload, th):
        return f"[SKIP] {workload} {th}TH (既存)"
    return _run_job_inner(workload, binary, args_template, timeout_sec, th, prefix)


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
    jobs = []
    for name, binary, args_template, timeout_sec, arg_values in WORKLOADS:
        for th in arg_values:
            jobs.append((name, binary, args_template, timeout_sec, th))

    print(f"[gettsuushin_S] Size{SIZE}取得ジョブ数: {len(jobs)} (並列度={MAX_PARALLEL})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = [pool.submit(run_job, *j) for j in jobs]
        for fut in concurrent.futures.as_completed(futures):
            print(fut.result(), flush=True)

    print("[gettsuushin_S] 完了")


if __name__ == "__main__":
    main()
