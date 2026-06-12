# ミエルーム 開発ルール

## マニュアル更新（必須・毎回）

機能の追加・変更・削除をしたら、**同じ作業の中で必ずヘルプマニュアルも更新する**こと。コミット前にチェックする。更新対象は2箇所：

1. **`templates/guide.html`** — スクリーンショット付きの使い方ガイド（/guide）。
   - 該当機能のセクション（`<section class="feat">`）の手順・説明を実際の画面と一致させる
   - 新機能はセクション追加、`data-k` 属性に検索キーワードも追加する
2. **`app.py` の `MANUAL_SECTIONS`** — ヘルプの検索用マニュアル（/api/manual）。
   - 該当エントリの body を更新、なければ追加する
3. **`static/manual/*.png`** — ガイドのスクリーンショット。画面の見た目が変わったページは撮り直す。
   - ローカルサーバー起動（`python app.py`）後、`python _shoot.py` で全ページ自動撮影（デモアカウントでログインして撮る）。モーダルは `python _shoot_modal.py`
   - 変更ページだけ撮り直す場合も _shoot.py の PAGES リストを使う

UIの文言変更・ボタン追加・タブ名変更など、ユーザーの操作手順や見た目が変わるものはすべて対象。内部実装のみの変更（リファクタ・バグ修正で画面が変わらないもの）は不要。

## デプロイ

- main へ push すると Railway に自動デプロイされる（「デプロイしないで」と言われない限り、変更後は毎回 commit & push する）
- ローカル起動: `python app.py`（ポート5000）

## 権限まわり

- 左サイドバー各機能の表示権限は `app.py` の `NAV_PERM_GROUPS`（中央カタログ）で一元管理
- 機能ページを追加したら: ①NAV_PERM_GROUPS に項目追加 ②AppUser にカラム追加 ③SQLite/PostgreSQL 両方のマイグレーションに追加 ④ルートに `@nav_perm_required('can_view_xxx')` ⑤mgmt_base.html のナビを `user_perms.get(...)` で囲む
