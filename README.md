# X240C I2C Logger

PowerPi X240C の I2C 通信を記録・解析する Python 製ロガーです。現在のバージョンは **0.4.4** です。
PicoXTools の WebSocket、保存済みのテキストログ、RP2040 PIO スニファーのシリアル出力を入力として、レジスタ操作や OTG 電圧・推定電流を CSV に保存します。

## ハードウェアの入手先

- [PowerPi X240C（Taobao）](https://item.taobao.com/item.htm?id=1071762544986)
- [PicoXtools（Taobao）](https://item.taobao.com/item.htm?id=895785154737)

## 主な機能

- PicoXTools のバイナリイベントを受信し、メッセージ境界をまたぐ I2C トランザクションを復元
- Read、Write、repeated START を含む複数フェーズの解析
- ACK/NACK の記録（Read の最終バイトの NACK は正常として扱います）
- 既定の 7 ビットアドレス `0x6C` に対するレジスタ解析
- 物理トランザクション単位の CSV と、電圧変更に関連する操作をまとめた SET 集計 CSV の出力

## 必要な環境とセットアップ

Python 3.9 以降と、利用する入力方式に対応した機器またはログを用意してください。
PicoXTools を使う場合は、PC から機器の WebSocket エンドポイント `ws://<host>/ws/i2c` に接続できる必要があります。
RP2040 のファームウェアや機器の配線手順は、このリポジトリには含まれていません。

```powershell
git clone https://github.com/inuchanbt/x240c-i2c-logger.git
cd x240c-i2c-logger
py -m pip install -r requirements.txt
```

以下は Windows の `py` を使う例です。他の環境では `python3` など、お使いの Python コマンドに読み替えてください。
`websocket-client` は WebSocket 入力、`pyserial` はシリアル入力とポート一覧の表示に使用します。

## 使い方

### PicoXTools に直接接続

```powershell
py x240c_i2c_logger.py --picotools
py x240c_i2c_logger.py --picotools 192.168.33.1
```

ホスト省略時は `192.168.33.1` に接続します。Windows では `run_picotools.bat` からも起動できます。
別のホストや追加オプションを指定する例は `run_picotools.bat 192.168.33.2 --ws-debug` です。
バッチファイルは自身のあるフォルダに移動してから起動します。

### 保存済みテキストログを解析

```powershell
py x240c_i2c_logger.py --input capture.txt --out logs/replay.csv
```

入力には PicoXTools のコピー済みテキストログなど、スニファー形式のテキストを指定します。出力 CSV をそのまま再入力する機能ではありません。PicoXTools 形式では `14:36:00.000 S 6C W 04 D3 02 P` のように時刻付きの行を使用してください。時刻なしの `S 6C ...` は RP2040 形式として判定されます。

### RP2040 のシリアル出力を記録

```powershell
py x240c_i2c_logger.py --list-ports
py x240c_i2c_logger.py --port COM14 --baud 115200
```

入力方式を指定しない場合は標準入力を読み込みます。ライブ記録は `Ctrl+C` で終了します。

## 出力ファイル

既定では、実行時の作業フォルダの `logs/` に次のファイルを作成します。出力先フォルダは自動作成されます。

| ファイル | 内容 |
| --- | --- |
| `x240c_i2c_YYYYMMDD_HHMMSS.csv` | 物理トランザクションごとの時刻、I2C データ、解析結果、フラグ、元データ |
| `x240c_i2c_YYYYMMDD_HHMMSS_sets.csv` | 電圧変更を起点にまとめた操作、推定電流、トランザクション数、終了理由など |

CSV は UTF-8 BOM 付きです。`--out logs/capture.csv` を指定すると、集計先は既定で `logs/capture_sets.csv` になります。
repeated START を含む物理トランザクションは複数の論理操作に分かれるため、SET の `transaction_count` と `operation_count` は異なる場合があります。

| オプション | 内容・既定値 |
| --- | --- |
| `--out PATH` | トランザクション CSV の保存先 |
| `--sets-out PATH` | SET 集計 CSV の保存先 |
| `--no-sets` | SET 集計を無効化 |
| `--set-idle-ms 300` | SET の基本部分を待機状態に移す無通信時間（ms） |
| `--set-tail-window-ms 5000` | 遅れて到着する関連操作を待つ時間（ms） |
| `--set-tail-lead-ms 250` | DCDC 無効化直前の電流設定を関連付ける時間（ms） |
| `--addr 0x6C` | 対象の 7 ビット I2C アドレス |
| `--rsense-mohm 10` | 電流推定に使う実効センス抵抗（mΩ） |
| `--ws-debug` | WebSocket のバイナリメッセージを表示 |
| `--append` | 既存 CSV に追記 |
| `--quiet` | 解析結果のコンソール表示を抑制 |

全オプションは `py x240c_i2c_logger.py --help` で確認できます。

## 解析上の注意

WebSocket のバイナリ形式とレジスタの解釈には、実測ログから推定した内容が含まれます。未解明のビットはフラグや元データとして残します。
電圧はレジスタ `0x04` と `0x05` の設定値から復号した値で、実測値ではありません。
レジスタ `0x06` による電流値も推定値です。X240C のセンス抵抗によるスケーリングは実機での確認が必要です。

v0.4.4 では、同じ物理トランザクションに「前の SET の電流設定」と「次の SET の電圧設定」が含まれる場合の集計境界を修正しています。
具体的な再現例は [v0.4.4 の技術メモ（英語）](docs/v0.4.4-notes.md) を参照してください。

## フォルダ構成

```text
x240c-i2c-logger/
├── x240c_i2c_logger.py  # ロガー本体
├── run_picotools.bat    # Windows 用起動スクリプト
├── requirements.txt    # Python 依存ライブラリ
├── tests/              # 回帰テスト
├── docs/               # 技術メモ
├── logs/               # 計測 CSV（Git 管理対象外）
├── README.md
└── LICENSE
```

手元の旧版 ZIP は `archives/` に保管し、計測ログとともに Git 管理対象から除外しています。

## 動作確認

外部機器なしで、SET 集計の回帰テストを実行できます。

```powershell
py tests/test_v044.py
py x240c_i2c_logger.py --help
```

回帰テストでは repeated START の SET 境界、別トランザクションの電流の誤結合防止、遅延した DCDC 関連操作の集計を確認します。

## ライセンス

[MIT License](LICENSE)
