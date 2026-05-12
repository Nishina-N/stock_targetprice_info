# Price Target Tracker

Finviz からアナリストの目標株価変更を自動取得し、GitHub Pages でダッシュボード表示する。

## 構成

```
.
├── .github/workflows/fetch.yml     # GitHub Actions（毎時実行）
├── scripts/
│   ├── fetch_price_targets.py      # データ取得スクリプト
│   └── metadata_target_stocks_latest.csv  # ウォッチリスト銘柄
├── docs/
│   ├── index.html                  # GitHub Pages ダッシュボード
│   └── data.json                   # 取得データ（自動生成）
└── requirements.txt
```

## セットアップ

1. このリポジトリを GitHub に push する
2. Settings → Pages → Source を `docs/` フォルダに設定
3. Actions タブで `Fetch Price Targets` ワークフローを手動実行して初回データを生成
4. 以降は毎時0分（UTC）に自動更新される

## ダッシュボード機能

- ティッカー・会社名・セクター・インダストリー表示
- Upgrade / Downgrade / Reiterated / Initiated バッジ
- 目標株価の変化率（%）表示
- ティッカー・アナリスト・セクターでフィルタリング
- PT上昇 / 下落フィルター
- 全カラムソート対応
- 1時間ごとに自動リフレッシュ
