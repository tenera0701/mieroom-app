# Railwayデプロイ手順（初回）

## ① GitHubにコードを上げる（10分）

1. https://github.com でアカウント作成（無料）

2. 右上「+」→「New repository」
   - Repository name: `roompick-tool`（または任意の名前）
   - **Private** を選択（コードを非公開に）
   - Create repository

3. PCにGitをインストール（未インストールの場合）
   - https://git-scm.com/download/win からダウンロード・インストール

4. このフォルダ（realestate-ai）でコマンドプロンプトを開いて実行：

```
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/roompick-tool.git
git push -u origin main
```

---

## ② Railwayにデプロイする（10分）

1. https://railway.app でアカウント作成（GitHubでログイン推奨）

2. 「New Project」→「Deploy from GitHub repo」
   → `roompick-tool` を選択

3. PostgreSQLデータベースを追加：
   - プロジェクト内「+ New」→「Database」→「PostgreSQL」

4. 環境変数を設定（Variables タブ）：
   ```
   SECRET_KEY = (ランダムな文字列を設定。例: roompick-secret-2024-xyz)
   ```
   ※ DATABASE_URL は PostgreSQL追加時に自動設定される

5. デプロイが完了すると URL が発行される（例：https://roompick-tool.railway.app）

---

## ③ スタッフに共有する

発行されたURLをLINEやSlackでスタッフに送るだけ。

初回ログイン:
- ユーザー名: owner
- パスワード: roompick2024

→ 設定画面でスタッフアカウントを追加してください。

---

## ツール販売する場合

新規顧客に提供するとき：
1. Railwayで新しいプロジェクトを作成（同じリポジトリを使用）
2. 客ごとに別のURLが発行される
3. 初期パスワードを伝えて使い始められる

月額コスト（1社あたり）: 約500〜700円/月

---

## コード変更後の更新方法

ローカルで修正したら：
```
git add .
git commit -m "修正内容"
git push
```
→ Railwayが自動でデプロイし直す（約1〜2分）
