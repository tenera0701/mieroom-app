# Gmail 即時通知（Pub/Sub Push）設定手順

メール到着の**瞬間**にGoogleからミエルームへ通知が届き、即座に反響取込＋自動返信が走るようになります。
（設定しなくても2秒間隔のチェックで動作します。設定すると“ほぼ同時”になります）

## 前提
- すでにGoogle連携（メール送受信のOAuth設定）が済んでいること
- その際に使った **Google Cloud プロジェクト** にアクセスできること

---

## 手順（所要15分くらい）

### 1. Google Cloud Console を開く
https://console.cloud.google.com を開き、画面上部のプロジェクト選択で
**OAuthクライアント（GOOGLE_CLIENT_ID）を作ったのと同じプロジェクト**を選びます。

### 2. Pub/Sub API を有効化
- 左メニュー「APIとサービス」→「ライブラリ」
- 「Cloud Pub/Sub API」を検索 →「有効にする」

### 3. トピックを作成
- 左メニュー「Pub/Sub」→「トピック」→「トピックを作成」
- トピックID：`mieroom-mail`（任意の名前でOK）
- そのまま「作成」

### 4. Gmailからの通知を許可（重要）
- 作成したトピックを開く →「権限」タブ →「プリンシパルを追加」
- 新しいプリンシパル：`gmail-api-push@system.gserviceaccount.com`
- ロール：「Pub/Sub パブリッシャー」
- 「保存」

### 5. Push サブスクリプションを作成
- トピックの画面下「サブスクリプションを作成」
- サブスクリプションID：`mieroom-mail-push`
- 配信タイプ：**Push**
- エンドポイントURL：
  ```
  https://＜アプリのドメイン＞/api/gmail-push?key=＜好きな長いランダム文字列＞
  ```
  例：`https://mieroom-app-production.up.railway.app/api/gmail-push?key=Xy7kP2mQ9rT4wZ8a`
  ※`key=` の値はパスワードのようなもの。長めのランダム文字列を自分で決めてメモしておく
- 「作成」

### 6. Railway に環境変数を追加
Railway のプロジェクト → Variables に以下の2つを追加：

| 変数名 | 値 |
|---|---|
| `GMAIL_PUBSUB_TOPIC` | `projects/＜プロジェクトID＞/topics/mieroom-mail` |
| `GMAIL_PUSH_KEY` | 手順5で決めたランダム文字列 |

※プロジェクトIDは Google Cloud Console の上部に表示されているID（例：`mieroom-123456`）

保存するとRailwayが自動で再起動します。

---

## 動作確認
Railway のログ（Deployments → View Logs）で確認：

1. 起動から約1分以内に `gmail watch ok store=N` が出る → 監視登録OK
2. テストメールを送ると、ほぼ同時に `gmail push: instant fetch store=N` が出る → 即時通知が機能！
3. 反響管理表に数秒でNEWバッジ付きで表示され、自動返信も即送信される

## 補足
- 監視は約7日で失効しますが、アプリが12時間ごとに自動更新するので放置でOK
- この設定をしていなくても、2秒間隔のチェック（最大5秒前後）で動き続けます
- Google連携を新しいメールアドレスでやり直した場合も、自動で監視に含まれます
