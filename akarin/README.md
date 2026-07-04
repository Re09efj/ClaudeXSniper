# AKARIN システム

DeLoc/MPO(Jinの2段階ヒューリスティック)を、CP-SAT・Optuna・TabPFNの3つで
置き換える統合マッピングシステム。名前の由来はユーザーの好きなアニメキャラ。

## 全体パイプライン

```
[既知ワークロード群]
    ↓ Optuna探索(Sniper実測がオラクル)
最適 alpha の教師データ
    ↓
[新規ワークロード] → プロファイリング(comm.csv/mem_access.csv)
    → 特徴量抽出 → TabPFN → alpha予測 → CP-SAT解 → cpu_map
```

## 各コンポーネントの役割

- **`cpsat_mapper.py`**: OR-Tools CP-SATで、通信局所性(remote_penalty)とヘテロ性
  (makespan = 各コアの完了時間の最大値)を1つの目的関数
  `alpha*makespan + (1-alpha)*remote_penalty` として同時最適化する。
  DeLoc/MPOの「ノード決定→ノード内Big/Small」という順序固定の2段階ヒューリスティックとは異なり、
  両方を同時に見るため、ノード制約によるBig core枠の溢れ問題（Jin論文3.3.1節の失敗例）が
  構造的に起きにくい。alphaは外側のループ(Optuna)が決める前提の設計。
- **`optuna_search.py`** (未実装): あるワークロードについて、alpha候補ごとにCP-SATでcpu_mapを
  計算し、実際にSniperで走らせてsim_time_msを測定、その実測値をフィードバックにalphaを探索する。
  Sniperが「本当の目的関数」を教えてくれるオラクルとして機能する。
- **`train_tabpfn.py`** (未実装): Optunaが既知ワークロードについて求めた最適alphaを正解ラベルに、
  ワークロードの特徴量(imbalance_ratio、疎ペア割合、thread0のハブ集中度など)からalphaを回帰予測する
  TabPFNモデルを学習する。学習後は新規ワークロードに対し、Optunaの再探索なしで1回の
  プロファイリング+TabPFN予測+CP-SAT実行だけでcpu_mapを得られる。

## 依存関係

`cpsat_mapper.py`は`utility/deloc_mapper.py`のデータ読み込み関数
(`load_comm_matrix`, `_pairs_from_matrix`, `load_mem_access`, `mem_access_path_for`,
`compute_load_imbalance`)とトポロジ定数(`NUM_NODES`, `CORES_PER_NODE`,
`NODE_P_CORES`, `NODE_E_CORES`)を再利用する。`deloc_mapper.py`自体はJinの本物のMPO
（Packed/Scatter/HPO/EPOとの比較用ベースライン）としても単独で使うため、`utility/`側に
留めてある（AKARIN専用にはしていない）。

## 単体実行

```bash
python3 -m akarin.cpsat_mapper Data/comm_matrices/lavaMD_A_12TH_lavaMD.12.6.comm.csv --threads 12 --alpha 0.5
```
