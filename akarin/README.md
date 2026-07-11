# AKARIN システム

DeLoc/MPO(Jinの2段階ヒューリスティック)を、OR-Tools CP-SATによる単一の同時最適化で
置き換えるマッピングシステム。名前の由来はユーザーの好きなアニメキャラ。

## 全体パイプライン

```
[ワークロード] → プロファイリング(comm.csv/comm_size.csv/mem_access.csv)
    → CP-SAT(ルーフラインモデル、決定的な単一解) → cpu_map
```

2026-07-11: 当初は`alpha*makespan + (1-alpha)*remote_penalty`という、Optunaで
alphaを探索する設計だったが、以下2点の実測検証を経て全面刷新した:
- 7/6: remote_penalty(「別ノードだから悪い」という前提)は実測と逆相関(-0.438)、
  「同ノード内でDRAMコントローラ帯域を奪い合う」という競合モデルの方が正相関
  (+0.339)と判明
- 7/10: First-Touch実装(v3)によりノードをまたぐアクセスに構造的コストが乗る
  ようになったため、ノード間バス(システム全体で1本のみ共有)の競合項を追加

alphaという恣意的な重み付けパラメータが不要なルーフラインモデル
(3種のボトルネック候補のうち最も遅いものがそのまま完了時間になる、という考え方)
に置き換えたことで、探索対象の無くなった`optuna_search.py`/`train_tabpfn.py`は廃止した。

## 各コンポーネントの役割

- **`cpsat_mapper.py`**: OR-Tools CP-SATで、以下のルーフライン式を最小化する
  cpu_mapを一意に求める(既存5戦略Packed/Scatter/HPO/EPO/MPOとの比較用候補として使う):
  ```
  node_finish[n] = max(compute_bound[n], dram_bound[n])
  total_finish   = max(max_n(node_finish[n]), bus_bound)
  ```
  - `compute_bound[n]`: ノード内で一番遅いスレッドの実行時間
    (mem_access.csv実測バイト数÷コア周波数の粗い代理指標)
  - `dram_bound[n]`: ノード全体の合計メモリアクセス量÷DRAMコントローラ実帯域
    (`PER_CONTROLLER_BANDWIDTH_GBPS`, config/generate_config.py)
  - `bus_bound`: ノードをまたぐ通信量(comm_size.csv実測バイト数、システム全体で合算)
    ÷ノード間バス実帯域(`BUS_BANDWIDTH_GBPS`)
  DeLoc/MPOの「ノード決定→ノード内Big/Small」という順序固定の2段階ヒューリスティックとは異なり、
  局所性とヘテロ性を同時に見るため、ノード制約によるBig core枠の溢れ問題（Jin論文3.3.1節の
  失敗例）が構造的に起きにくい。
- **`generate_candidates.py`**: 上記CP-SAT解(AKARIN候補、1点のみ)と既存5戦略の
  cpu_mapをまとめ、正規化署名で重複排除する。Sniperは実行しない(高速・低負荷)。

## 依存関係

`cpsat_mapper.py`は`utility/deloc_mapper.py`のデータ読み込み関数
(`load_comm_matrix`, `_pairs_from_matrix`, `load_mem_access`, `mem_access_path_for`,
`comm_size_path_for`, `compute_load_imbalance`)とトポロジ定数(`NUM_NODES`, `CORES_PER_NODE`,
`NODE_P_CORES`, `NODE_E_CORES`)を再利用する。`deloc_mapper.py`自体はJinの本物のMPO
（Packed/Scatter/HPO/EPOとの比較用ベースライン）としても単独で使うため、`utility/`側に
留めてある（AKARIN専用にはしていない）。

帯域定数(`PER_CONTROLLER_BANDWIDTH_GBPS`, `BUS_BANDWIDTH_GBPS`)とP/E周波数
(`P_FREQ`, `E_FREQ`)は`config/generate_config.py`から直接importする(Sniperが実際に
シミュレートする値とAKARINの最適化計算に使う値がズレる事故が過去に起きたため、
単一の真実源から取る設計)。

## 単体実行

```bash
python3 -m akarin.cpsat_mapper Data/tsuushin/sizeS/BT_S_8TH_bt.S.x.8.6.comm.csv --threads 8
```
