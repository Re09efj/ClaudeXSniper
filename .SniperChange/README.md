# .SniperChange

`docker.io/snipersim/snipersim:latest`(vanilla Sniper、無改造)から現行本番
イメージ`localhost/snipersim/snipersim:detloc-firsttouch-v12-dedupfix`
(`utility/sniper_sim_sid.py`の`CONTAINER_IMAGE`参照)までの、**全ての差分
ファイル**を元のSniperソースツリー相対パスのまま保存している。

ClaudeXSniper自体のgit管理下に置くことで、別リポジトリを作らずに変更履歴を
残す(2026-07-13)。vanilla Sniperと2つの画像(vanilla/v12-dedupfix)から
`podman cp`でソースツリー全体を取り出し、`diff -rq`で機械的に差分検出した
結果に基づく一覧なので、手作業での見落としはない。

lib/sniper(コンパイル済みバイナリ)と`tools/*.pyc`(バイトコードキャッシュ)は
ビルド成果物であり再生成可能なため対象外。

## 変更ファイル一覧(全21件)

### First-Touch実装(v2/v3、Sniper移行時の本命機能)

- `common/core/memory_subsystem/address_home_lookup.cc` / `.h`
  AddressHomeLookupへのFirst-Touch実装本体。「本棚問題」
  (getHome()がアドレスハッシュのみでコアの物理配置を無視する問題)への対応。
  v3でstatic共有マップに変更(v2はコアごとに別インスタンスで
  first-touch記録がバラバラだった重大バグの修正)。

- `common/core/memory_subsystem/parametric_dram_directory_msi/cache_cntlr.cc` / `.h`
  First-Touch後の検証用計測ポイント(access-home-local/remote)を追加。

- `common/core/memory_subsystem/pr_l1_pr_l2_dram_directory_msi/dram_directory_cntlr.cc`
  DRAM_LOCAL/REMOTE判定のノード単位化。

- `common/network/network_model_bus.cc` / `.h`
  NetworkModelBusのignore_local_traffic判定をFirst-Touchに対応。

### pinned_mapスケジューラ(Jinのパッチ、Sniper移行の前提)

- `common/scheduler/scheduler.cc`
  pinned_mapスケジューラの統合点。`scheduler.cc.orig`が改造前の原本。

- `common/scheduler/scheduler_pinned_map.cc` / `.h`
  GOMP_CPU_AFFINITYがこの環境で機能しない問題への対応。map_file
  (thread_id:cpu_id)による厳密な静的コア配置を実現する新規スケジューラ。

- `common/scheduler/split_string.cc` / `.h`
  scheduler_pinned_mapが使う文字列分割ユーティリティ(新規)。

### 今回のバグ調査での修正(2026-07-12〜13)

- `common/misc/setlock.h`
  `_SetLock`(`PersetLock`)がpthread_mutexの所有権UB(あるスレッドが
  acquire_exclusive()でロックしたスロットを、別スレッドがrelease_shared()で
  解放していた)を踏んでいた問題を修正。futexベースの実装に置き換え。

- `common/core/memory_subsystem/cache/cache_block_info.h`
  `m_tag`/`m_cstate`をmemory_order_acquire/releaseからseq_cst(atomic既定)に
  変更。tag→cstateの書き込み順序とread順序が一致しない、複数変数間の
  IRIW的なギャップを解消。

- `common/system/barrier_sync_server.h` / `.cc`
  `m_global_time`/`m_next_barrier_time`の共有フィールドに、atomicなミラー
  フィールドを追加。5箇所の書き込みサイトで同期。

- `common/trace_frontend/trace_manager.h` / `.cc`
  - `.h`: `m_num_threads_started`を`UInt32`から`std::atomic<UInt32>`に変更。
  - `.cc`: `TraceManager::signalDone()`の「アプリの残りスレッドがちょうど
    1本になったら強制resume」というfluidanimate向け修正(2026-07-06)を、
    「アプリの残り(未停止)スレッド全員がCore::STALLED状態なら強制resume」
    へ一般化。dedupのようなパイプライン型ワークロードで、複数スレッドが
    同時に(1本ではなく)stallするケースに対応(2026-07-12)。

- `common/misc/subsecond_time.h`
  `atomic_set_subsecondtime`/`atomic_get_subsecondtime`/
  `atomic_update_max_subsecondtime`のatomicヘルパー関数を追加。

- `common/performance_model/shmem_perf_model.cc`
  `ShmemPerfModel::setElapsedTime`/`getElapsedTime`/`updateElapsedTime`を、
  上記のatomicヘルパー経由に書き換え。

## 対応関係

| 修正 | 対象バグ/機能 | 検証 |
|---|---|---|
| address_home_lookup.*, cache_cntlr.*(検証計測点), dram_directory_cntlr.cc, network_model_bus.* | First-Touch実装(本棚問題) | GUPSで実測検証済み(Packed=100%ローカル等)、Documents/2026年7月10日.md参照 |
| scheduler.cc(.orig), scheduler_pinned_map.*, split_string.* | GOMP_CPU_AFFINITY非対応への対応(pinned_mapスケジューラ) | Sniper移行初期に導入、以後全実験の基盤 |
| setlock.h, cache_block_info.h, barrier_sync_server.*, trace_manager.h, subsecond_time.h, shmem_perf_model.cc | LU/2TH/Wの silent hang | v9で確認済み(60分超の継続的健全進行、Documents/SniperBugFix.md参照) |
| trace_manager.cc の signalDone一般化 | dedupのTRACE段階での複数スレッド同時ハング | v12-dedupfixでDEDUP/W/9TH/Packed完走を確認済み(1413.55秒) |

## 差分の再現方法(今後の更新時)

```bash
# vanilla側
CID=$(podman create docker.io/snipersim/snipersim:latest true)
podman cp "$CID:/root/sniper" /tmp/vanilla_sniper
podman rm "$CID"

# 現行本番イメージ側
CID=$(podman create localhost/snipersim/snipersim:<現行タグ> true)
podman cp "$CID:/root/sniper" /tmp/current_sniper
podman rm "$CID"

# 差分検出(lib/sniper・.pyc・pin_kit等のビルド成果物は除外)
diff -rq /tmp/vanilla_sniper /tmp/current_sniper \
  --exclude=".git" --exclude="*.o" --exclude="*.d" --exclude="*.a" --exclude="*.so" \
  --exclude="pin_kit" --exclude="obj-intel64" --exclude="__pycache__"
```
