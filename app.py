# ======================================================
# DBリセット手順:
#   PLRecordモデルに新カラムを追加したため、
#   既存のDBには新カラムが存在しない。
#   リセットするには instance/realestate.db を削除して再起動する。
#   例: del instance\realestate.db (Windows)
# ======================================================
import os
import json
import random
import tempfile
import re
import socket
import threading
import time
import hashlib
import imaplib
import email as emaillib
from email.header import decode_header as _decode_header
from functools import wraps
from flask import Flask, redirect, url_for, session, render_template, request, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import anthropic
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

# Railway(PostgreSQL) or local(SQLite) の自動切り替え
_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgres://"):          # Railway が postgres:// を返す場合
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url or "sqlite:///realestate.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_IS_POSTGRES = bool(_db_url)

db = SQLAlchemy(app)


@app.before_request
def sync_session_role():
    """セッションのロールをDBと同期（再ログイン不要でロール変更を反映）"""
    uid = session.get('app_user_id')
    if uid:
        user = AppUser.query.get(uid)
        if user and user.is_active:
            session['app_user_role'] = user.role
        elif user and not user.is_active:
            session.clear()


@app.after_request
def add_no_cache(response):
    """HTMLページとAPI(JSON)応答のブラウザキャッシュを無効化。
    APIをキャッシュすると編集後の再取得が古い値を返し、リロードするまで反映されない。"""
    ctype = response.content_type or ''
    is_api = request.path.startswith('/api/')
    if 'text/html' in ctype or 'application/json' in ctype or is_api:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.context_processor
def inject_ui_context():
    """全テンプレートに共通変数を注入（is_premium はリクエスト時に評価）"""
    def _is_premium():
        uid = session.get('app_user_id')
        if not uid:
            return False
        u = AppUser.query.get(uid)
        if not u or u.role == 'super_admin' or not u.tenant_id:
            return False
        t = Tenant.query.get(u.tenant_id)
        return bool(t and t.plan == 'premium')

    def _sidebar_stores():
        """サイドバー用店舗リスト（店舗切替セレクト・本部リンク判定に使用）
        ignore_active=True で全店舗を返す（切替中の店舗に関わらず全店表示）"""
        uid = session.get('app_user_id')
        if not uid:
            return []
        u = AppUser.query.get(uid)
        if not u or u.role == 'super_admin':
            return []
        if u.role == 'owner':
            return Store.query.filter_by(tenant_id=u.tenant_id, is_active=True).all()
        # store_manager / staff: 自分の店舗のみ
        if u.store_id:
            s = Store.query.get(u.store_id)
            return [s] if s and s.is_active else []
        return []

    def _current_user_perms():
        uid = session.get('app_user_id')
        if not uid:
            return {}
        u = AppUser.query.get(uid)
        if not u:
            return {}
        return {
            'can_view_executive':   getattr(u, 'can_view_executive', True),
            'can_view_leads_page':  getattr(u, 'can_view_leads_page', True),
            'can_view_daily_report':getattr(u, 'can_view_daily_report', True),
            'can_view_leave':       getattr(u, 'can_view_leave', True),
            'can_view_accounting':  getattr(u, 'can_view_accounting', True),
        }

    return {
        'is_premium': _is_premium(),
        'current_role': session.get('app_user_role', ''),
        'sidebar_stores': _sidebar_stores(),
        'user_perms': _current_user_perms(),
    }


anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── 既存モデル ─────────────────────────────────────────────

# ── 幹部向け管理ツール：追加モデル ────────────────────────

class Tenant(db.Model):
    """テナント（契約会社）マスタ — マルチSaaS用"""
    __tablename__ = 'tenant'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)       # 会社名
    plan = db.Column(db.String(20), default='standard')   # standard / premium
    is_active = db.Column(db.Boolean, default=True)
    trial_ends_at = db.Column(db.DateTime, nullable=True)  # トライアル終了日時
    subscription_status = db.Column(db.String(20), default='trial')  # trial / active / locked / cancelled
    contract_start_date = db.Column(db.Date, nullable=True)  # 契約開始日
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Store(db.Model):
    """店舗マスタ"""
    __tablename__ = 'store'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    # 月次固定費（円）
    rent = db.Column(db.Float, default=0)           # 家賃
    parking_fee = db.Column(db.Float, default=0)    # 駐車場代
    copier_fee = db.Column(db.Float, default=0)     # コピー機リース
    internet_fee = db.Column(db.Float, default=0)   # インターネット
    consultant_fee = db.Column(db.Float, default=0) # コンサル費
    insurance_fee = db.Column(db.Float, default=0)  # 保険料
    cloud_fee = db.Column(db.Float, default=0)      # クラウド・SaaS
    is_active = db.Column(db.Boolean, default=True)
    is_locked = db.Column(db.Boolean, default=False)           # 店舗ロック
    contract_start_date = db.Column(db.Date, nullable=True)    # 店舗契約開始日
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Staff(db.Model):
    """スタッフマスタ"""
    __tablename__ = 'staff'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'))
    role = db.Column(db.String(50), default='営業')
    is_active = db.Column(db.Boolean, default=True)
    hired_at = db.Column(db.Date)


class SalesKPI(db.Model):
    """月次営業KPI"""
    __tablename__ = 'sales_kpi'
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    inquiries = db.Column(db.Integer, default=0)      # 反響数
    store_visits = db.Column(db.Integer, default=0)   # 来店数
    viewings = db.Column(db.Integer, default=0)       # 内見数
    applications = db.Column(db.Integer, default=0)   # 申込数
    contracts = db.Column(db.Integer, default=0)      # 契約数
    cancellations = db.Column(db.Integer, default=0)  # キャンセル数
    sales_amount = db.Column(db.Float, default=0)     # 売上（円）
    option_sales = db.Column(db.Float, default=0)     # オプション売上（円）
    estimated_sales = db.Column(db.Float, default=0)     # 売上見込み（円）
    target_sales = db.Column(db.Float, default=0)        # 月次目標売上（円）
    fire_insurance_count = db.Column(db.Integer, default=0)  # 火災保険件数
    lifeline_count = db.Column(db.Integer, default=0)        # ライフライン件数
    moving_count = db.Column(db.Integer, default=0)          # 引越し件数


class Lead(db.Model):
    """反響（リード）管理"""
    __tablename__ = 'lead'
    id = db.Column(db.Integer, primary_key=True)
    # 反響媒体：SUUMO, HOME'S, at home, Instagram, TikTok, Google, LINE, HP, 電話, MEO
    source = db.Column(db.String(50))
    received_at = db.Column(db.DateTime, default=datetime.utcnow)
    # ステータス：未対応, 対応中, 来店, 内見, 申込, 契約, 不成立
    status = db.Column(db.String(50), default='未対応')
    assigned_staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'))
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'))
    customer_name = db.Column(db.String(100))
    note = db.Column(db.Text)
    line_added = db.Column(db.Boolean, default=False)


class LeadMediaStat(db.Model):
    """媒体別月次反響統計"""
    __tablename__ = 'lead_media_stat'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    media = db.Column(db.String(50), nullable=False)
    inquiries = db.Column(db.Integer, default=0)       # 反響数
    replies = db.Column(db.Integer, default=0)         # 返信
    line_added = db.Column(db.Integer, default=0)      # LINE追加
    visits = db.Column(db.Integer, default=0)          # 来店
    applications = db.Column(db.Integer, default=0)    # 申込
    contracts = db.Column(db.Integer, default=0)       # 契約
    cancellations = db.Column(db.Integer, default=0)   # キャンセル
    cancel_amount = db.Column(db.Float, default=0)     # キャンセル金額
    estimated_sales = db.Column(db.Float, default=0)   # 売上見込み
    actual_payment = db.Column(db.Float, default=0)    # 入金
    ad_cost = db.Column(db.Float, default=0)           # 広告費


class AdCost(db.Model):
    """月次広告費"""
    __tablename__ = 'ad_cost'
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(50), nullable=False)  # 媒体名
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    cost = db.Column(db.Float, default=0)              # 広告費（円）
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'))


class PLRecord(db.Model):
    """月次PL（損益計算）"""
    __tablename__ = 'pl_record'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    revenue = db.Column(db.Float, default=0)         # 売上
    gross_profit = db.Column(db.Float, default=0)    # 粗利
    ad_cost = db.Column(db.Float, default=0)         # 広告費
    labor_cost = db.Column(db.Float, default=0)      # 人件費
    other_fixed = db.Column(db.Float, default=0)     # その他固定費
    other_variable = db.Column(db.Float, default=0)  # その他変動費
    # 収入詳細
    brokerage_fee = db.Column(db.Float, default=0)        # 仲介手数料
    ad_income = db.Column(db.Float, default=0)            # AD収入
    lifeline_income = db.Column(db.Float, default=0)      # ライフライン収入
    moving_income = db.Column(db.Float, default=0)        # 引越収入
    fire_insurance_income = db.Column(db.Float, default=0) # 火災保険収入
    other_income = db.Column(db.Float, default=0)         # その他収入
    # 広告費詳細
    suumo_cost = db.Column(db.Float, default=0)
    homes_cost = db.Column(db.Float, default=0)
    athome_cost = db.Column(db.Float, default=0)
    instagram_cost = db.Column(db.Float, default=0)
    tiktok_cost = db.Column(db.Float, default=0)
    google_ads_cost = db.Column(db.Float, default=0)
    line_cost = db.Column(db.Float, default=0)
    hp_cost = db.Column(db.Float, default=0)
    meo_cost = db.Column(db.Float, default=0)
    other_ad_cost = db.Column(db.Float, default=0)
    # 人件費詳細（commission_payカラムを社会保険料として再利用）
    regular_salary = db.Column(db.Float, default=0)    # 正社員人件費
    parttime_salary = db.Column(db.Float, default=0)   # アルバイト人件費
    commission_pay = db.Column(db.Float, default=0)    # 社会保険料（旧:歩合給）
    # 固定費詳細
    pl_rent = db.Column(db.Float, default=0)           # 家賃
    pl_parking = db.Column(db.Float, default=0)        # 駐車場
    pl_copier = db.Column(db.Float, default=0)         # 複合機
    pl_internet = db.Column(db.Float, default=0)       # インターネット
    pl_consultant = db.Column(db.Float, default=0)     # 顧問料
    pl_insurance = db.Column(db.Float, default=0)      # 保険料
    pl_cloud = db.Column(db.Float, default=0)          # クラウド


class PLCustomItem(db.Model):
    """PLカスタム費用項目テンプレート（固定費・変動費・その他）"""
    __tablename__ = 'pl_custom_item'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(20), default='固定費')  # 固定費 / 変動費 / その他
    sort_order = db.Column(db.Integer, default=0)


class PLCustomValue(db.Model):
    """PLカスタム費用項目の月次金額"""
    __tablename__ = 'pl_custom_value'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(20), default='固定費')
    amount = db.Column(db.Float, default=0)


class UncollectedPayment(db.Model):
    """未入金（AD）管理"""
    __tablename__ = 'uncollected_payment'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    property_name = db.Column(db.String(200))     # 物件名
    room_number   = db.Column(db.String(50))       # 合室
    application_date    = db.Column(db.Date)        # 申込日
    management_company  = db.Column(db.String(200)) # 管理会社名
    customer_name       = db.Column(db.String(100)) # お客様名
    expected_payment_date = db.Column(db.Date)      # 入金予定日
    amount = db.Column(db.Float, default=0)
    memo   = db.Column(db.Text)
    is_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AppUser(db.Model):
    """管理ツールログインユーザー"""
    __tablename__ = 'app_user'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=True)  # super_admin=None
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(200), nullable=True)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='staff')  # 'super_admin'/'owner'/'store_manager'/'staff'
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    # 権限フラグ（store_manager/staff向け）
    can_view_accounting = db.Column(db.Boolean, default=True)
    can_view_all_staff = db.Column(db.Boolean, default=True)
    can_edit_kpi = db.Column(db.Boolean, default=True)
    can_manage_uncollected = db.Column(db.Boolean, default=True)
    # ページアクセス権限（ナビ項目ごと）
    can_view_executive = db.Column(db.Boolean, default=True)      # 売上管理
    can_view_leads_page = db.Column(db.Boolean, default=True)     # 反響管理
    can_view_daily_report = db.Column(db.Boolean, default=True)   # 日報
    can_view_leave = db.Column(db.Boolean, default=True)          # 有給管理
    # クライアント管理画面の操作権限（sys_admin向け）
    admin_can_add_tenant    = db.Column(db.Boolean, default=False)  # 新規テナント追加
    admin_can_manage_stores = db.Column(db.Boolean, default=False)  # 店舗管理
    admin_can_delete_tenant = db.Column(db.Boolean, default=False)  # 削除
    admin_can_lock_tenant   = db.Column(db.Boolean, default=False)  # ロック・解除


class PasswordResetToken(db.Model):
    """パスワードリセットトークン"""
    __tablename__ = 'password_reset_token'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('app_user.id'), nullable=False)
    token = db.Column(db.String(256), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LeaveRecord(db.Model):
    """有給・休暇管理"""
    __tablename__ = 'leave_record'
    id = db.Column(db.Integer, primary_key=True)
    store_id  = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    staff_id  = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    leave_date = db.Column(db.Date, nullable=False)
    leave_type = db.Column(db.String(20), default='有給')  # 有給/半休/欠勤/遅刻/早退/その他
    days       = db.Column(db.Float, default=1.0)          # 日数（0.5=半日）
    memo       = db.Column(db.Text)
    status     = db.Column(db.String(20), default='承認済')  # 申請中/承認済/却下
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LeaveBalance(db.Model):
    """年次有給日数管理"""
    __tablename__ = 'leave_balance'
    id = db.Column(db.Integer, primary_key=True)
    store_id      = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    staff_id      = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    year          = db.Column(db.Integer, nullable=False)
    total_days    = db.Column(db.Float, default=10.0)    # 付与日数（法定またはカスタム）
    carryover_days= db.Column(db.Float, default=0.0)     # 前年繰越日数
    is_custom     = db.Column(db.Boolean, default=False) # True=手動設定 / False=法定自動計算
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


class DailyTaskTemplate(db.Model):
    """日報タスクテンプレート（店舗ごとにカスタム可能）"""
    __tablename__ = 'daily_task_template'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    task_name = db.Column(db.String(200), nullable=False)
    is_default = db.Column(db.Boolean, default=False)  # デフォルトタスク
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)


class DailyReport(db.Model):
    """日報"""
    __tablename__ = 'daily_report'
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    report_date = db.Column(db.Date, nullable=False)
    # タスクチェック（固定）
    prev_day_contact_done = db.Column(db.Boolean, default=False)   # 来店前日連絡
    same_day_contact_done = db.Column(db.Boolean, default=False)   # 来店当日連絡
    application_input_done = db.Column(db.Boolean, default=False)  # 申込管理入力
    # 申込数
    application_count = db.Column(db.Integer, default=0)
    # 明日の接客予定
    tomorrow_appointments = db.Column(db.Text)
    # メモ
    memo = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyReportCustomer(db.Model):
    """日報の接客記録"""
    __tablename__ = 'daily_report_customer'
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('daily_report.id'), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    applied = db.Column(db.Boolean, default=False)          # 申込になったか
    no_apply_reason = db.Column(db.Text)                    # 申込にならなかった理由
    improvement = db.Column(db.Text)                        # 改善案


class DailyTaskCheck(db.Model):
    """日報のカスタムタスクチェック"""
    __tablename__ = 'daily_task_check'
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('daily_report.id'), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey('daily_task_template.id'), nullable=False)
    checked = db.Column(db.Boolean, default=False)


class ContractRecord(db.Model):
    """申込・契約台帳（Excelインポートデータ）"""
    __tablename__ = 'contract_record'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'))
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'))
    staff_name_raw = db.Column(db.String(100))       # Excelのシート名
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    seq_no = db.Column(db.Integer)
    status = db.Column(db.String(50))                # 契約/申込/キャンセル
    application_date = db.Column(db.Date)
    media = db.Column(db.String(100))
    property_name = db.Column(db.String(200))
    room_no = db.Column(db.String(50))
    customer_name = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    rent = db.Column(db.Float)
    management_company = db.Column(db.String(200))
    review_status = db.Column(db.String(100))
    doc_arrival_date = db.Column(db.Date)
    contract_visit_date = db.Column(db.Date)
    settlement_date = db.Column(db.Date)
    contract_start_date = db.Column(db.Date)
    ad_income_date_raw = db.Column(db.String(100))   # テキスト含む
    ad_income_date = db.Column(db.Date)
    commission_pct = db.Column(db.Float)
    other_cost = db.Column(db.Float)
    ad_pct = db.Column(db.Float)
    ad_received = db.Column(db.String(10))           # ○/×
    lifeline = db.Column(db.String(10))
    moving = db.Column(db.String(10))
    fire_insurance = db.Column(db.String(10))
    application_amount = db.Column(db.Float, default=0)
    sales_amount = db.Column(db.Float, default=0)
    contract_amount = db.Column(db.Float, default=0)
    cancel_type = db.Column(db.String(50))
    source_file = db.Column(db.String(500))
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)


class MediaType(db.Model):
    """媒体マスター（申込一覧のプルダウン用）"""
    __tablename__ = 'media_type'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    name = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)


class StatusColor(db.Model):
    """申込ステータス別行カラー設定"""
    __tablename__ = 'status_color'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), default=1)
    status_key = db.Column(db.String(50), nullable=False)
    bg_color = db.Column(db.String(20), default='#ffffff')      # バッジ背景色
    text_color = db.Column(db.String(20), default='#111827')    # 文字色
    row_bg_color = db.Column(db.String(20), default='#ffffff')  # 行全体の背景色


class ApplicationRecord(db.Model):
    """申込一覧表"""
    __tablename__ = 'application_record'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey('store.id'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    application_date = db.Column(db.Date, nullable=False)
    media = db.Column(db.String(100))
    property_name = db.Column(db.String(200))
    room_number = db.Column(db.String(50))
    customer_name = db.Column(db.String(100))
    rent = db.Column(db.Float, default=0)
    contract_start_date = db.Column(db.Date)
    ad_payment_date = db.Column(db.Date)
    brokerage_fee = db.Column(db.Float, default=0)
    ancillary_services = db.Column(db.Text)  # 旧フィールド（廃止・後方互換のため保持）
    option_amount = db.Column(db.Float, default=0)
    ad_type = db.Column(db.String(10), default='amount')  # 'amount' or 'percent'
    ad_amount = db.Column(db.Float, default=0)
    lifeline = db.Column(db.Boolean, default=False)
    moving = db.Column(db.Boolean, default=False)
    fire_insurance = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default='申込')   # 申込/契約/キャンセル/キャンセル振替
    # 入金承認ワークフロー
    ad_settled = db.Column(db.Boolean, default=False)          # 営業がAD入金報告
    ad_approved = db.Column(db.Boolean, default=False)         # 店長がAD承認
    brokerage_settled = db.Column(db.Boolean, default=False)   # 営業が仲介入金報告
    brokerage_approved = db.Column(db.Boolean, default=False)  # 店長が仲介承認
    brokerage_payment_date = db.Column(db.Date, nullable=True) # 仲介入金日
    # その他費用（旧オプション）の入金承認ワークフロー（仲手とは別方向）
    option_settled = db.Column(db.Boolean, default=False)      # 営業がその他費用入金報告
    option_approved = db.Column(db.Boolean, default=False)     # 店長がその他費用承認
    option_payment_date = db.Column(db.Date, nullable=True)    # その他費用入金日
    management_company = db.Column(db.String(200))             # 管理会社名
    review_ng = db.Column(db.Boolean, default=False)          # 審査×（True=審査NG→キャンセル）旧フィールド
    review_status = db.Column(db.String(10), nullable=True)  # 審査状態: None=—, 'ok'=○, 'ng'=×
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContractDocument(db.Model):
    """契約書類（取引成立台帳など）の編集データ。申込1件につき1つ。data はJSON文字列。"""
    __tablename__ = 'contract_document'
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('application_record.id'), unique=True, nullable=False)
    store_id = db.Column(db.Integer)
    data = db.Column(db.Text)   # JSON
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FloorPlan(db.Model):
    """間取り（編集可能なキャンバスデータ。data は fabric.js のJSON）"""
    __tablename__ = 'floor_plan'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer)
    name = db.Column(db.String(200))
    data = db.Column(db.Text)   # fabric.js canvas JSON（背景画像のbase64含む）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EchoRecord(db.Model):
    """反響管理表（追客進捗管理）"""
    __tablename__ = 'echo_record'
    id            = db.Column(db.Integer, primary_key=True)
    store_id      = db.Column(db.Integer, db.ForeignKey('store.id'))
    staff_id      = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    list_name     = db.Column(db.String(200))   # リスト名
    echo_date     = db.Column(db.Date)           # 反響日
    media         = db.Column(db.String(100))    # 媒体
    method        = db.Column(db.String(100))    # 手段
    first_contact_date = db.Column(db.Date, nullable=True)  # 初回対応日
    followup_1    = db.Column(db.Date, nullable=True)
    followup_2    = db.Column(db.Date, nullable=True)
    followup_3    = db.Column(db.Date, nullable=True)
    followup_4    = db.Column(db.Date, nullable=True)
    followup_5    = db.Column(db.Date, nullable=True)
    followup_6    = db.Column(db.Date, nullable=True)
    followup_7    = db.Column(db.Date, nullable=True)
    followup_8    = db.Column(db.Date, nullable=True)
    followup_9    = db.Column(db.Date, nullable=True)
    followup_10   = db.Column(db.Date, nullable=True)
    has_reply     = db.Column(db.Boolean, default=False)  # 返信有
    has_phone     = db.Column(db.Boolean, default=False)  # 電話対応有無
    has_line      = db.Column(db.Boolean, default=False)  # LINE追加
    memo          = db.Column(db.Text)
    external_id   = db.Column(db.String(160), nullable=True)  # 反響メール一意ID（重複取込防止）
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


class MailSetting(db.Model):
    """店舗ごとの反響メール自動取込設定（IMAP）"""
    __tablename__ = 'mail_setting'
    id               = db.Column(db.Integer, primary_key=True)
    store_id         = db.Column(db.Integer, db.ForeignKey('store.id'), unique=True)
    imap_host        = db.Column(db.String(120), default='imap.gmail.com')
    imap_user        = db.Column(db.String(200))   # 連携するGmailアドレス
    imap_pass        = db.Column(db.String(200))   # アプリパスワード
    enabled          = db.Column(db.Boolean, default=False)  # 自動取込ON/OFF
    default_staff_id = db.Column(db.Integer, nullable=True)  # 取込時のデフォルト担当
    custom_keywords  = db.Column(db.Text)                    # 追加判定キーワード（1行=「語」or「語=媒体名」）
    last_fetch_at    = db.Column(db.DateTime, nullable=True)
    last_result      = db.Column(db.String(300))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomerServiceRecord(db.Model):
    """接客管理表"""
    __tablename__ = 'customer_service_record'
    id            = db.Column(db.Integer, primary_key=True)
    store_id      = db.Column(db.Integer, db.ForeignKey('store.id'))
    card_no       = db.Column(db.String(50))    # カードNo
    service_date  = db.Column(db.Date)          # 日付
    echo_media    = db.Column(db.String(100))   # 反響媒体
    staff_id      = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    customer_name = db.Column(db.String(100))   # お客様名
    service_type  = db.Column(db.String(50))    # 対応種別
    visit_count   = db.Column(db.Integer, default=0)  # 来店数
    status        = db.Column(db.String(20), default='追客中')  # 追客中/申込/他決/キャンセル
    memo          = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


class DropdownOption(db.Model):
    """プルダウン選択肢マスタ（テナント別・カテゴリ別）"""
    __tablename__ = 'dropdown_option'
    id         = db.Column(db.Integer, primary_key=True)
    tenant_id  = db.Column(db.Integer, nullable=True)          # NULL=全テナント共通デフォルト
    category   = db.Column(db.String(50),  nullable=False)     # echo_media / echo_method / cs_media / cs_service_type / leads_media
    value      = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, default=0)


class TrialApplication(db.Model):
    """トライアル申込フォーム送信履歴"""
    __tablename__ = 'trial_application'
    id         = db.Column(db.Integer, primary_key=True)
    company    = db.Column(db.String(200), nullable=False)
    name       = db.Column(db.String(100), nullable=False)
    email      = db.Column(db.String(200), nullable=False)
    phone      = db.Column(db.String(50),  nullable=False)
    stores     = db.Column(db.String(20),  nullable=False)
    message    = db.Column(db.Text,        nullable=True)
    status     = db.Column(db.String(20),  default='new')   # new / contacted / contracted / rejected
    memo       = db.Column(db.Text,        nullable=True)   # 管理者メモ
    created_at = db.Column(db.DateTime,    default=datetime.utcnow)


# ── Excel関連ヘルパー ─────────────────────────────────────

def excel_date_to_date(v):
    """ExcelシリアルまたはdatetimeをPython dateに変換"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and v > 1:
        return (date(1899, 12, 30) + timedelta(days=int(v)))
    if hasattr(v, 'date'):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def to_float_safe(v):
    """安全にfloat変換、失敗時はNone"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def to_str_safe(v):
    """安全にstr変換"""
    if v is None:
        return None
    return str(v).strip() if str(v).strip() else None


def import_excel_file(file_path_or_stream, year, month, store_id=1):
    """
    Excelファイルを読んでContractRecordとSalesKPIを更新する。
    戻り値: {"imported": N, "staff_results": [...], "errors": [...]}
    """
    try:
        import openpyxl
    except ImportError:
        return {"imported": 0, "staff_results": [], "errors": ["openpyxl がインストールされていません"]}

    errors = []
    staff_results = []
    total_imported = 0

    try:
        if isinstance(file_path_or_stream, str):
            wb = openpyxl.load_workbook(file_path_or_stream, data_only=True)
        else:
            wb = openpyxl.load_workbook(file_path_or_stream, data_only=True)
    except Exception as e:
        return {"imported": 0, "staff_results": [], "errors": [f"Excelファイル読み込みエラー: {str(e)}"]}

    # テンプレート・集計シートは除外
    SKIP_SHEETS = {'原本', 'Sheet1', 'Sheet2', 'Sheet3', '集計', 'サマリー', '目標', 'テンプレート'}

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        staff_name = sheet_name.strip()

        # テンプレートシートをスキップ
        if staff_name in SKIP_SHEETS or staff_name.startswith('Sheet'):
            continue

        # スタッフ検索・新規作成
        staff = Staff.query.filter_by(name=staff_name).first()
        if not staff:
            staff = Staff(name=staff_name, store_id=store_id, role='営業', is_active=True)
            db.session.add(staff)
            db.session.flush()

        # 同月の既存ContractRecordを削除（上書き）
        ContractRecord.query.filter_by(
            staff_id=staff.id, year=year, month=month
        ).delete()
        db.session.flush()

        sheet_imported = 0
        sheet_errors = []

        # 行12からデータ開始、2行1セット
        max_row = ws.max_row
        r = 12
        while r <= max_row:
            try:
                # 奇数行（メイン行）
                odd_row = r
                even_row = r + 1

                # 顧客名チェック（列7）
                customer_name = to_str_safe(ws.cell(odd_row, 7).value)
                if not customer_name:
                    r += 2
                    continue

                # ステータスチェック（列2）
                status_raw = to_str_safe(ws.cell(odd_row, 2).value)
                if not status_raw:
                    r += 2
                    continue

                # ---- 奇数行データ ----
                seq_no_v = ws.cell(odd_row, 1).value
                seq_no = int(seq_no_v) if seq_no_v and str(seq_no_v).isdigit() else None

                application_date = excel_date_to_date(ws.cell(odd_row, 4).value)
                property_name = to_str_safe(ws.cell(odd_row, 6).value)
                rent_v = to_float_safe(ws.cell(odd_row, 8).value)
                management_company = to_str_safe(ws.cell(odd_row, 9).value)
                commission_pct = to_float_safe(ws.cell(odd_row, 18).value)
                other_cost = to_float_safe(ws.cell(odd_row, 19).value)
                ad_pct = to_float_safe(ws.cell(odd_row, 20).value)
                ad_received = to_str_safe(ws.cell(odd_row, 21).value)
                lifeline = to_str_safe(ws.cell(odd_row, 22).value)
                moving = to_str_safe(ws.cell(odd_row, 23).value)
                fire_insurance = to_str_safe(ws.cell(odd_row, 24).value)
                application_amount = to_float_safe(ws.cell(odd_row, 34).value) or 0
                sales_amount_v = to_float_safe(ws.cell(odd_row, 35).value) or 0
                contract_amount = to_float_safe(ws.cell(odd_row, 36).value) or 0
                cancel_type = to_str_safe(ws.cell(odd_row, 48).value)

                # ---- 偶数行データ ----
                source_raw = to_str_safe(ws.cell(even_row, 4).value)   # 接客ソース
                media = to_str_safe(ws.cell(even_row, 5).value)
                room_no = to_str_safe(ws.cell(even_row, 6).value)
                phone = to_str_safe(ws.cell(even_row, 7).value)
                review_status = to_str_safe(ws.cell(even_row, 9).value)
                doc_arrival_date = excel_date_to_date(ws.cell(even_row, 10).value)
                contract_visit_date = excel_date_to_date(ws.cell(even_row, 11).value)
                settlement_date = excel_date_to_date(ws.cell(even_row, 12).value)
                contract_start_date = excel_date_to_date(ws.cell(even_row, 14).value)

                # AD入金日（テキスト含む場合あり）
                ad_date_raw_v = ws.cell(even_row, 15).value
                ad_income_date_raw = to_str_safe(ad_date_raw_v)
                ad_income_date = excel_date_to_date(ad_date_raw_v)

                record = ContractRecord(
                    store_id=store_id,
                    staff_id=staff.id,
                    staff_name_raw=staff_name,
                    year=year,
                    month=month,
                    seq_no=seq_no,
                    status=status_raw,
                    application_date=application_date,
                    media=media,
                    property_name=property_name,
                    room_no=room_no,
                    customer_name=customer_name,
                    phone=phone,
                    rent=rent_v,
                    management_company=management_company,
                    review_status=review_status,
                    doc_arrival_date=doc_arrival_date,
                    contract_visit_date=contract_visit_date,
                    settlement_date=settlement_date,
                    contract_start_date=contract_start_date,
                    ad_income_date_raw=ad_income_date_raw,
                    ad_income_date=ad_income_date,
                    commission_pct=commission_pct,
                    other_cost=other_cost,
                    ad_pct=ad_pct,
                    ad_received=ad_received,
                    lifeline=lifeline,
                    moving=moving,
                    fire_insurance=fire_insurance,
                    application_amount=application_amount,
                    sales_amount=sales_amount_v,
                    contract_amount=contract_amount,
                    cancel_type=cancel_type,
                )
                db.session.add(record)
                sheet_imported += 1

            except Exception as e:
                sheet_errors.append(f"行{r}: {str(e)}")

            r += 2

        db.session.flush()

        # シートごとのKPI集計
        records_this_sheet = ContractRecord.query.filter_by(
            staff_id=staff.id, year=year, month=month
        ).all()

        applications_cnt = sum(1 for rc in records_this_sheet if rc.status in ('申込', '契約'))
        contracts_cnt = sum(1 for rc in records_this_sheet if rc.status == '契約')
        cancellations_cnt = sum(1 for rc in records_this_sheet if rc.status == 'キャンセル')
        sales_amt = sum(
            (rc.contract_amount if rc.contract_amount else (rc.rent or 0))
            for rc in records_this_sheet if rc.status == '契約'
        )
        option_sales = (
            sum(1 for rc in records_this_sheet if rc.lifeline and '○' in rc.lifeline) * 5000 +
            sum(1 for rc in records_this_sheet if rc.moving and '○' in rc.moving) * 3000
        )

        # KPI更新
        kpi = SalesKPI.query.filter_by(staff_id=staff.id, store_id=store_id,
                                        year=year, month=month).first()
        if not kpi:
            kpi = SalesKPI(staff_id=staff.id, store_id=store_id, year=year, month=month)
            db.session.add(kpi)
        kpi.applications = applications_cnt
        kpi.contracts = contracts_cnt
        kpi.cancellations = cancellations_cnt
        kpi.sales_amount = sales_amt
        kpi.option_sales = float(option_sales)
        db.session.flush()

        total_imported += sheet_imported
        staff_results.append({
            'staff_name': staff_name,
            'staff_id': staff.id,
            'imported': sheet_imported,
            'contracts': contracts_cnt,
            'applications': applications_cnt,
            'cancellations': cancellations_cnt,
            'errors': sheet_errors,
        })

    db.session.commit()
    return {
        'imported': total_imported,
        'staff_results': staff_results,
        'errors': errors,
    }


# ── 初期店舗・スタッフ作成関数 ────────────────────────────

def init_store():
    """
    Storeテーブルが空のときだけデフォルトテナント・店舗・スタッフを作成する。
    """
    if Store.query.count() > 0:
        return

    print("初期テナント・店舗・スタッフを作成しています...")

    # デフォルトテナント
    tenant = Tenant.query.first()
    if not tenant:
        tenant = Tenant(name='ルームピック', plan='standard', is_active=True)
        db.session.add(tenant)
        db.session.flush()

    store = Store(name='ルームピック', is_active=True, tenant_id=tenant.id)
    db.session.add(store)
    db.session.flush()

    for i in range(1, 4):
        staff = Staff(name=f'スタッフ{i}', store_id=store.id, role='営業', is_active=True)
        db.session.add(staff)

    db.session.commit()
    init_default_media_types(store.id)
    print("初期テナント・店舗・スタッフの作成が完了しました。")

    # 初期オーナーアカウント作成
    if AppUser.query.count() == 0:
        owner = AppUser(
            username='owner',
            password_hash=generate_password_hash('roompick2024'),
            role='owner',
            tenant_id=tenant.id,
        )
        db.session.add(owner)
        db.session.commit()
        print("初期オーナーアカウントを作成しました。(username: owner)")
    return


def ensure_super_admin():
    """super_adminアカウントを確実に作成・維持する"""
    sa = AppUser.query.filter_by(username='super_admin').first()
    if not sa:
        sa = AppUser(
            username='super_admin',
            email='teneramente0701@gmail.com',
            password_hash=generate_password_hash('SuperAdmin2024!'),
            role='super_admin',
            tenant_id=None,
            is_active=True,
        )
        db.session.add(sa)
        db.session.commit()
        print("super_adminアカウントを作成しました (username: super_admin)")
    elif sa.role != 'super_admin':
        sa.role = 'super_admin'
        sa.tenant_id = None
        sa.is_active = True
        db.session.commit()
        print("super_adminのロールを修正しました")


def init_default_media_types(store_id):
    """デフォルト媒体マスターを初期化（新規店舗時）"""
    defaults = ['SUUMO', "HOME'S", 'at home', 'カナリー', 'スモッカ', 'HP', 'SNS', 'リピート', '紹介', '飛び込み', '直電話問合せ']
    for i, name in enumerate(defaults):
        if not MediaType.query.filter_by(store_id=store_id, name=name).first():
            db.session.add(MediaType(store_id=store_id, name=name, sort_order=i, is_active=True))
    db.session.commit()


def ensure_owner_account():
    """AppUserが存在しない場合にオーナーアカウントを作成する"""
    if AppUser.query.count() == 0:
        tenant = Tenant.query.first()
        owner = AppUser(
            username='owner',
            password_hash=generate_password_hash('roompick2024'),
            role='owner',
            tenant_id=tenant.id if tenant else None,
        )
        db.session.add(owner)
        db.session.commit()
        print("初期オーナーアカウントを作成しました。(username: owner)")


# ── DB初期化 & 初期データ作成 ──────────────────────────────

def migrate_db():
    """
    既存DBに新カラムを追加するマイグレーション。
    SQLiteはALTER TABLE ADD COLUMN をサポートしている。
    """
    import sqlite3
    db_path = os.path.join(app.instance_path, 'realestate.db')
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # pl_record テーブルの新カラムを追加
    pl_new_columns = [
        ('brokerage_fee', 'FLOAT DEFAULT 0'),
        ('ad_income', 'FLOAT DEFAULT 0'),
        ('lifeline_income', 'FLOAT DEFAULT 0'),
        ('moving_income', 'FLOAT DEFAULT 0'),
        ('fire_insurance_income', 'FLOAT DEFAULT 0'),
        ('other_income', 'FLOAT DEFAULT 0'),
        ('suumo_cost', 'FLOAT DEFAULT 0'),
        ('homes_cost', 'FLOAT DEFAULT 0'),
        ('athome_cost', 'FLOAT DEFAULT 0'),
        ('instagram_cost', 'FLOAT DEFAULT 0'),
        ('tiktok_cost', 'FLOAT DEFAULT 0'),
        ('google_ads_cost', 'FLOAT DEFAULT 0'),
        ('line_cost', 'FLOAT DEFAULT 0'),
        ('hp_cost', 'FLOAT DEFAULT 0'),
        ('meo_cost', 'FLOAT DEFAULT 0'),
        ('other_ad_cost', 'FLOAT DEFAULT 0'),
        ('regular_salary', 'FLOAT DEFAULT 0'),
        ('parttime_salary', 'FLOAT DEFAULT 0'),
        ('commission_pay', 'FLOAT DEFAULT 0'),
        ('pl_rent', 'FLOAT DEFAULT 0'),
        ('pl_parking', 'FLOAT DEFAULT 0'),
        ('pl_copier', 'FLOAT DEFAULT 0'),
        ('pl_internet', 'FLOAT DEFAULT 0'),
        ('pl_consultant', 'FLOAT DEFAULT 0'),
        ('pl_insurance', 'FLOAT DEFAULT 0'),
        ('pl_cloud', 'FLOAT DEFAULT 0'),
    ]

    # 既存カラムを取得
    cursor.execute("PRAGMA table_info(pl_record)")
    existing = {row[1] for row in cursor.fetchall()}

    for col_name, col_def in pl_new_columns:
        if col_name not in existing:
            try:
                cursor.execute(f"ALTER TABLE pl_record ADD COLUMN {col_name} {col_def}")
                print(f"  Added column pl_record.{col_name}")
            except Exception as e:
                print(f"  Skip {col_name}: {e}")

    # pl_custom_item / pl_custom_value の item_type カラム追加
    for tbl in ['pl_custom_item', 'pl_custom_value']:
        try:
            cursor.execute(f"PRAGMA table_info({tbl})")
            cols = {r[1] for r in cursor.fetchall()}
            if 'item_type' not in cols:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN item_type TEXT DEFAULT '固定費'")
                print(f"  Added column {tbl}.item_type")
        except Exception as e:
            print(f"  Skip {tbl}.item_type: {e}")

    # sales_kpi テーブルの新カラムを追加
    sales_kpi_new_cols = [
        ('estimated_sales',      'FLOAT DEFAULT 0'),
        ('target_sales',         'FLOAT DEFAULT 0'),
        ('fire_insurance_count', 'INTEGER DEFAULT 0'),
        ('lifeline_count',       'INTEGER DEFAULT 0'),
        ('moving_count',         'INTEGER DEFAULT 0'),
    ]
    cursor.execute("PRAGMA table_info(sales_kpi)")
    sk_existing = {row[1] for row in cursor.fetchall()}
    for col_name, col_def in sales_kpi_new_cols:
        if col_name not in sk_existing:
            try:
                cursor.execute(f"ALTER TABLE sales_kpi ADD COLUMN {col_name} {col_def}")
                print(f"  Added column sales_kpi.{col_name}")
            except Exception as e:
                print(f"  Skip sales_kpi.{col_name}: {e}")

    # app_user / store へ tenant_id カラムを追加
    for tbl in ['store', 'app_user']:
        cursor.execute(f"PRAGMA table_info({tbl})")
        cols = {r[1] for r in cursor.fetchall()}
        if 'tenant_id' not in cols:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN tenant_id INTEGER")
                print(f"  Added column {tbl}.tenant_id")
            except Exception as e:
                print(f"  Skip {tbl}.tenant_id: {e}")

    # store / tenant の後続カラム（migrate_postgres と整合させる）
    for tbl, extra_cols in [
        ('store', [
            ('is_locked', 'INTEGER DEFAULT 0'),
            ('contract_start_date', 'DATE'),
            ('created_at', 'TIMESTAMP'),
        ]),
        ('tenant', [
            ('trial_ends_at', 'TIMESTAMP'),
            ('subscription_status', "VARCHAR(20) DEFAULT 'trial'"),
            ('contract_start_date', 'DATE'),
        ]),
    ]:
        cursor.execute(f"PRAGMA table_info({tbl})")
        existing = {r[1] for r in cursor.fetchall()}
        for col_name, col_def in extra_cols:
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_def}")
                    print(f"  Added column {tbl}.{col_name}")
                except Exception as e:
                    print(f"  Skip {tbl}.{col_name}: {e}")

    # app_user の権限フラグカラムを追加
    cursor.execute("PRAGMA table_info(app_user)")
    au_cols = {r[1] for r in cursor.fetchall()}
    for col_name, col_def in [
        ('can_view_accounting',    'INTEGER DEFAULT 1'),
        ('can_view_all_staff',     'INTEGER DEFAULT 1'),
        ('can_edit_kpi',           'INTEGER DEFAULT 1'),
        ('can_manage_uncollected', 'INTEGER DEFAULT 1'),
        ('can_view_executive',     'INTEGER DEFAULT 1'),
        ('can_view_leads_page',    'INTEGER DEFAULT 1'),
        ('can_view_daily_report',  'INTEGER DEFAULT 1'),
        ('can_view_leave',         'INTEGER DEFAULT 1'),
        ('admin_can_add_tenant',    'INTEGER DEFAULT 0'),
        ('admin_can_manage_stores', 'INTEGER DEFAULT 0'),
        ('admin_can_delete_tenant', 'INTEGER DEFAULT 0'),
        ('admin_can_lock_tenant',   'INTEGER DEFAULT 0'),
    ]:
        if col_name not in au_cols:
            try:
                cursor.execute(f"ALTER TABLE app_user ADD COLUMN {col_name} {col_def}")
                print(f"  Added column app_user.{col_name}")
            except Exception as e:
                print(f"  Skip app_user.{col_name}: {e}")

    # application_record の新カラムを追加
    cursor.execute("PRAGMA table_info(application_record)")
    ar_cols = {r[1] for r in cursor.fetchall()}
    for col_name, col_def in [
        ('option_amount', 'FLOAT DEFAULT 0'),
        ('option_settled', 'BOOLEAN DEFAULT 0'),
        ('option_approved', 'BOOLEAN DEFAULT 0'),
        ('option_payment_date', 'DATE'),
        ('brokerage_payment_date', 'DATE'),
        ('management_company', 'VARCHAR(200)'),
        ('review_ng', 'BOOLEAN DEFAULT 0'),
        ('review_status', 'VARCHAR(10)'),
    ]:
        if col_name not in ar_cols:
            try:
                cursor.execute(f"ALTER TABLE application_record ADD COLUMN {col_name} {col_def}")
                print(f"  Added column application_record.{col_name}")
            except Exception as e:
                print(f"  Skip application_record.{col_name}: {e}")

    # status_color に row_bg_color を追加
    cursor.execute("PRAGMA table_info(status_color)")
    scs_cols = {r[1] for r in cursor.fetchall()}
    if 'row_bg_color' not in scs_cols:
        try:
            cursor.execute("ALTER TABLE status_color ADD COLUMN row_bg_color VARCHAR(20) DEFAULT '#ffffff'")
            print("  Added column status_color.row_bg_color")
        except Exception as e:
            print(f"  Skip status_color.row_bg_color: {e}")

    # customer_service_record の status カラムを追加
    cursor.execute("PRAGMA table_info(customer_service_record)")
    csr_cols = {r[1] for r in cursor.fetchall()}
    if 'status' not in csr_cols:
        try:
            cursor.execute("ALTER TABLE customer_service_record ADD COLUMN status VARCHAR(20) DEFAULT '追客中'")
            print("  Added column customer_service_record.status")
        except Exception as e:
            print(f"  Skip customer_service_record.status: {e}")

    # daily_report の store_id カラムを追加
    cursor.execute("PRAGMA table_info(daily_report)")
    dr_cols = {r[1] for r in cursor.fetchall()}
    if 'store_id' not in dr_cols:
        try:
            cursor.execute("ALTER TABLE daily_report ADD COLUMN store_id INTEGER")
            print("  Added column daily_report.store_id")
        except Exception as e:
            print(f"  Skip daily_report.store_id: {e}")

    # echo_record の external_id カラムを追加（反響メール重複取込防止）
    cursor.execute("PRAGMA table_info(echo_record)")
    er_cols = {r[1] for r in cursor.fetchall()}
    if 'external_id' not in er_cols:
        try:
            cursor.execute("ALTER TABLE echo_record ADD COLUMN external_id VARCHAR(160)")
            print("  Added column echo_record.external_id")
        except Exception as e:
            print(f"  Skip echo_record.external_id: {e}")

    # mail_setting の custom_keywords カラムを追加
    try:
        cursor.execute("PRAGMA table_info(mail_setting)")
        mscols = {r[1] for r in cursor.fetchall()}
        if mscols and 'custom_keywords' not in mscols:
            cursor.execute("ALTER TABLE mail_setting ADD COLUMN custom_keywords TEXT")
            print("  Added column mail_setting.custom_keywords")
    except Exception as e:
        print(f"  Skip mail_setting.custom_keywords: {e}")

    conn.commit()
    conn.close()


def migrate_postgres():
    """PostgreSQL用: 新カラムが存在しない場合のみALTER TABLEで追加
    各カラムを独立したコネクションで処理し、1つの失敗が他に影響しないようにする"""
    new_cols = [
        ("sales_kpi",          "estimated_sales",         "FLOAT DEFAULT 0"),
        ("sales_kpi",          "target_sales",            "FLOAT DEFAULT 0"),
        ("sales_kpi",          "fire_insurance_count",    "INTEGER DEFAULT 0"),
        ("sales_kpi",          "lifeline_count",          "INTEGER DEFAULT 0"),
        ("sales_kpi",          "moving_count",            "INTEGER DEFAULT 0"),
        ("store",              "tenant_id",               "INTEGER"),
        ("app_user",           "tenant_id",               "INTEGER"),
        ("app_user",           "email",                   "VARCHAR(200)"),
        ("app_user",           "store_id",                "INTEGER"),
        ("app_user",           "last_login",              "TIMESTAMP"),
        ("app_user",           "can_view_accounting",     "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_view_all_staff",      "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_edit_kpi",            "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_manage_uncollected",  "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_view_executive",      "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_view_leads_page",     "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_view_daily_report",   "BOOLEAN DEFAULT TRUE"),
        ("app_user",           "can_view_leave",          "BOOLEAN DEFAULT TRUE"),
        ("application_record",        "option_amount", "FLOAT DEFAULT 0"),
        ("application_record", "brokerage_payment_date", "DATE"),
        ("application_record", "option_settled", "BOOLEAN DEFAULT FALSE"),
        ("application_record", "option_approved", "BOOLEAN DEFAULT FALSE"),
        ("application_record", "option_payment_date", "DATE"),
        ("application_record", "management_company", "VARCHAR(200)"),
        ("application_record", "review_ng", "BOOLEAN DEFAULT FALSE"),
        ("application_record", "review_status", "VARCHAR(10)"),
        ("status_color", "row_bg_color", "VARCHAR(20) DEFAULT '#ffffff'"),
        ("daily_report",             "store_id",     "INTEGER"),
        ("customer_service_record",  "status",       "VARCHAR(20) DEFAULT '追客中'"),
        ("echo_record",              "external_id",  "VARCHAR(160)"),
        ("mail_setting",             "custom_keywords", "TEXT"),
        ("tenant", "trial_ends_at",        "TIMESTAMP"),
        ("tenant", "subscription_status",  "VARCHAR(20) DEFAULT 'trial'"),
        ("tenant", "contract_start_date",     "DATE"),
        ("store",  "created_at",             "TIMESTAMP"),
        ("store",  "is_locked",              "BOOLEAN DEFAULT FALSE"),
        ("store",  "contract_start_date",    "DATE"),
        ("app_user", "admin_can_add_tenant",    "BOOLEAN DEFAULT FALSE"),
        ("app_user", "admin_can_manage_stores", "BOOLEAN DEFAULT FALSE"),
        ("app_user", "admin_can_delete_tenant", "BOOLEAN DEFAULT FALSE"),
        ("app_user", "admin_can_lock_tenant",   "BOOLEAN DEFAULT FALSE"),
    ]
    # 各カラムを独立した接続で追加（1つの失敗が他に波及しない）
    for tbl, col, typedef in new_cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {typedef}"
                ))
                conn.commit()
                print(f"PG migrate OK: {tbl}.{col}")
        except Exception as e:
            print(f"PG migrate skip {tbl}.{col}: {e}")


_DROPDOWN_DEFAULTS = {
    'echo_media':      ['SUUMO', "HOME'S", 'アットホーム', 'カナリー', 'Instagram', 'TikTok', '自社HP', '電話', 'SNS', '紹介', 'その他'],
    'echo_method':     ['メール', '電話', 'LINE', 'チャット', 'その他'],
    'cs_media':        ['SUUMO', "HOME'S", 'アットホーム', 'カナリー', 'Instagram', 'TikTok', '自社HP', '電話', 'SNS', '紹介', 'その他'],
    'cs_service_type': ['来店', '電話', 'メール', 'オンライン', 'LINE', 'その他'],
    'cs_status':       ['追客中', '申込', '他決', 'キャンセル'],
    'leads_media':     ['SUUMO', "HOME'S", 'アットホーム', 'カナリー', 'Instagram', 'TikTok', '自社HP', '電話', 'SNS', '紹介', 'その他'],
}


def seed_dropdown_defaults():
    """各カテゴリにデフォルト選択肢が無い場合のみ挿入"""
    try:
        for cat, values in _DROPDOWN_DEFAULTS.items():
            if DropdownOption.query.filter_by(category=cat, tenant_id=None).count() == 0:
                for i, v in enumerate(values):
                    db.session.add(DropdownOption(tenant_id=None, category=cat, value=v, sort_order=i))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"seed_dropdown_defaults error: {e}")


def migrate_tenant_data():
    """既存データをデフォルトテナントに割り当てる（初回マイグレーション用）"""
    try:
        # テナントがなければ作成
        if Tenant.query.count() == 0:
            t = Tenant(name='ルームピック', plan='standard', is_active=True)
            db.session.add(t)
            db.session.commit()

        # ORDER BY id で決定的に最初のテナントを取得（PostgreSQLでも安定）
        default_tenant = Tenant.query.order_by(Tenant.id).first()

        # tenant_idのないstoreを割り当て
        for s in Store.query.filter_by(tenant_id=None).all():
            s.tenant_id = default_tenant.id

        # super_admin以外でtenant_idのないユーザーを割り当て
        for u in AppUser.query.filter_by(tenant_id=None).all():
            if u.role != 'super_admin':
                u.tenant_id = default_tenant.id

        db.session.commit()

        # オーナーのテナントに店舗がない場合は全アクティブ店舗を割り当て（データ整合性修復）
        for owner in AppUser.query.filter_by(role='owner').all():
            if owner.tenant_id:
                store_count = Store.query.filter_by(tenant_id=owner.tenant_id, is_active=True).count()
                if store_count == 0:
                    # このオーナーのテナントに属する店舗がない → デフォルトテナントの店舗を割り当て
                    for s in Store.query.filter_by(tenant_id=default_tenant.id, is_active=True).all():
                        s.tenant_id = owner.tenant_id
                    db.session.commit()
                    print(f"オーナー(id={owner.id})のテナント店舗を修復しました")

        # LeadMediaStat / Lead の store_id=NULL・0 を最初のアクティブ店舗に修復
        default_store = Store.query.filter_by(is_active=True).order_by(Store.id).first()
        if default_store:
            broken_stats = LeadMediaStat.query.filter(
                db.or_(LeadMediaStat.store_id == None, LeadMediaStat.store_id == 0)
            ).all()
            for s in broken_stats:
                s.store_id = default_store.id
            broken_leads = Lead.query.filter(
                db.or_(Lead.store_id == None, Lead.store_id == 0)
            ).all()
            for l in broken_leads:
                l.store_id = default_store.id
            if broken_stats or broken_leads:
                db.session.commit()
                print(f"store_id修復: LeadMediaStat={len(broken_stats)}件, Lead={len(broken_leads)}件")

        print("テナントデータのマイグレーション完了")

        # 既存店舗に媒体マスターがなければ初期化
        for s in Store.query.all():
            if MediaType.query.filter_by(store_id=s.id).count() == 0:
                init_default_media_types(s.id)
    except Exception as e:
        db.session.rollback()
        print(f"migrate_tenant_data error: {e}")


with app.app_context():
    db.create_all()
    if not _IS_POSTGRES:   # SQLite（ローカル）のみ
        migrate_db()
    else:
        migrate_postgres()
    init_store()
    ensure_owner_account()
    ensure_super_admin()
    migrate_tenant_data()
    seed_dropdown_defaults()
    # 無効なPLCustomItemテンプレートをクリーンアップ（数字のみの名前など）
    try:
        invalid_items = PLCustomItem.query.filter(
            db.or_(
                PLCustomItem.name.in_(['11', '1331', '13']),
                PLCustomItem.name == ''
            )
        ).all()
        for item in invalid_items:
            db.session.delete(item)
        if invalid_items:
            db.session.commit()
    except Exception:
        db.session.rollback()


# ── 認証デコレータ ────────────────────────────────────────

def is_tenant_locked(tenant):
    """テナントがロック/トライアル期限切れかどうかを判定"""
    if not tenant:
        return False
    if tenant.subscription_status == 'locked':
        return True
    if tenant.subscription_status == 'trial' and tenant.trial_ends_at:
        return datetime.utcnow() > tenant.trial_ends_at
    return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        # トライアル/ロックチェック（APIルートとロック通知ページ自身は除外）
        from flask import request as _req
        skip_paths = ('/trial-expired', '/app-login', '/forgot-password', '/reset-password')
        if not _req.path.startswith('/api/') and not any(_req.path.startswith(p) for p in skip_paths):
            user = AppUser.query.get(session['app_user_id'])
            if user and user.role != 'super_admin' and user.tenant_id:
                tenant = Tenant.query.get(user.tenant_id)
                if is_tenant_locked(tenant):
                    return redirect('/trial-expired')
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        user = AppUser.query.get(session['app_user_id'])
        if not user or user.role not in ('owner', 'super_admin'):
            return redirect(url_for('executive_dashboard'))
        return f(*args, **kwargs)
    return decorated


def owner_or_manager_required(f):
    """owner / store_manager / super_admin のみアクセス可（ログインアカウント管理など）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        user = AppUser.query.get(session['app_user_id'])
        if not user or user.role not in ('owner', 'store_manager', 'super_admin'):
            return redirect(url_for('executive_dashboard'))
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    """super_admin または sys_admin がアクセス可"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        if session.get('app_user_role') not in ('super_admin', 'sys_admin'):
            return redirect(url_for('executive_dashboard'))
        return f(*args, **kwargs)
    return decorated


def _check_admin_perm(perm_field):
    """sys_admin の場合、指定したadmin権限フィールドを確認して403を返す（持てばOK）"""
    role = session.get('app_user_role')
    if role == 'super_admin':
        return None  # super_adminは常にOK
    if role == 'sys_admin':
        user = AppUser.query.get(session.get('app_user_id'))
        if user and getattr(user, perm_field, False):
            return None  # 権限あり
        from flask import jsonify as _j
        return _j({'error': 'この操作の権限がありません'}), 403
    from flask import jsonify as _j
    return _j({'error': 'この操作の権限がありません'}), 403


def super_admin_only(f):
    """破壊的操作：super_admin のみ（後方互換のため残す）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        if session.get('app_user_role') != 'super_admin':
            from flask import jsonify as _jsonify
            return _jsonify({'error': 'この操作はスーパー管理者のみ実行できます'}), 403
        return f(*args, **kwargs)
    return decorated


def manager_or_above_required(f):
    """store_manager / owner のみアクセス可。staff と super_admin はブロック。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        user = AppUser.query.get(session['app_user_id'])
        if not user:
            return redirect(url_for('app_login'))
        if user.role == 'super_admin':
            return redirect(url_for('admin_tenants'))
        if user.role == 'staff':
            return redirect(url_for('sales_management'))
        return f(*args, **kwargs)
    return decorated


def block_super_admin(f):
    """ビジネス系ページから super_admin を弾く（テナント管理のみ許可）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('app_user_role') == 'super_admin':
            return redirect(url_for('admin_tenants'))
        return f(*args, **kwargs)
    return decorated


def is_premium_user():
    """現在のログインユーザーのテナントがプレミアプランかどうかを返す。
    super_admin はテナントを持たないため False。
    """
    uid = session.get('app_user_id')
    if not uid:
        return False
    user = AppUser.query.get(uid)
    if not user or user.role == 'super_admin' or not user.tenant_id:
        return False
    tenant = Tenant.query.get(user.tenant_id)
    return bool(tenant and tenant.plan == 'premium')


# ── ユーティリティ ─────────────────────────────────────────

def current_ym():
    """現在の年・月をタプルで返す"""
    now = datetime.now()
    return now.year, now.month


def get_allowed_store_ids(ignore_active=False):
    """ログインユーザーが参照できる店舗IDリストを返す（テナント分離）
    ignore_active=True の場合はセッションの active_store_id を無視して全店舗返す（本部ダッシュボード用）
    """
    uid = session.get('app_user_id')
    if not uid:
        return []
    user = AppUser.query.get(uid)
    if not user:
        return []
    if user.role == 'super_admin':
        return [s.id for s in Store.query.filter_by(is_active=True).order_by(Store.id).all()]
    elif user.role == 'owner':
        all_ids = [s.id for s in Store.query.filter_by(tenant_id=user.tenant_id, is_active=True).order_by(Store.id).all()]
        if not all_ids:
            null_stores = Store.query.filter(
                Store.tenant_id == None, Store.is_active == True
            ).order_by(Store.id).all()
            if null_stores:
                all_ids = [s.id for s in null_stores]
        if not ignore_active:
            active = session.get('active_store_id')
            if active and active in all_ids:
                return [active]
        return all_ids
    else:
        if user.store_id:
            return [user.store_id]
        if user.tenant_id:
            ids = [s.id for s in Store.query.filter_by(tenant_id=user.tenant_id, is_active=True).order_by(Store.id).all()]
            return ids[:1]
        return []


def get_allowed_stores(ignore_active=False):
    """ログインユーザーが参照できるStoreオブジェクトリストを返す"""
    uid = session.get('app_user_id')
    if not uid:
        return []
    user = AppUser.query.get(uid)
    if not user:
        return []
    if user.role == 'super_admin':
        return Store.query.filter_by(is_active=True).order_by(Store.id).all()
    elif user.role == 'owner':
        all_stores = Store.query.filter_by(tenant_id=user.tenant_id, is_active=True).order_by(Store.id).all()
        if not all_stores:
            all_stores = Store.query.filter(
                Store.tenant_id == None, Store.is_active == True
            ).order_by(Store.id).all()
        if not ignore_active:
            active = session.get('active_store_id')
            if active:
                filtered = [s for s in all_stores if s.id == active]
                if filtered:
                    return filtered
        return all_stores
    else:
        if user.store_id:
            s = Store.query.get(user.store_id)
            return [s] if s and s.is_active else []
        if user.tenant_id:
            return Store.query.filter_by(tenant_id=user.tenant_id, is_active=True).order_by(Store.id).limit(1).all()
        return []


def safe_store_id(requested_id=None):
    """テナント分離ヘルパー: リクエストの store_id を検証し安全な値を返す。
    - requested_id が許可範囲内なら そのまま返す
    - 無効 / 未指定なら active_store_id → 許可店舗の先頭 の順でフォールバック
    - 許可店舗がゼロなら None を返す（呼び出し側で 403 を返すこと）
    """
    allowed = get_allowed_store_ids()
    if not allowed:
        return None
    if requested_id and int(requested_id) in allowed:
        return int(requested_id)
    active = session.get('active_store_id')
    if active and active in allowed:
        return active
    return allowed[0]


# ── 既存ルーティング ──────────────────────────────────────

@app.route("/")
def index():
    # 管理ツールのログインページへリダイレクト
    return redirect(url_for('app_login'))


@app.route("/lp")
def lp():
    """ランディングページ（旧IeAI名義）"""
    return render_template("lp.html")


@app.route("/roompick-lp")
def roompick_lp():
    """ルームピック ランディングページ"""
    return render_template("roompick_lp.html")


@app.route("/app-login", methods=["GET", "POST"])
def app_login():
    """管理ツール専用ログイン"""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = AppUser.query.filter(
            db.or_(
                AppUser.username == username,
                db.func.lower(AppUser.email) == username.lower()
            ),
            AppUser.is_active == True
        ).first()
        if user and check_password_hash(user.password_hash, password):
            session.permanent = False  # ブラウザ閉じでセッション切れ
            session['app_user_id'] = user.id
            session['app_user_role'] = user.role
            session['app_username'] = user.username
            session['tenant_id'] = user.tenant_id
            user.last_login = datetime.utcnow()
            db.session.commit()
            # ログイン時に店舗を自動選択（複数店舗でも混在しないように）
            if user.role in ('owner', 'store_manager') and user.tenant_id:
                first_store = Store.query.filter_by(tenant_id=user.tenant_id, is_active=True).order_by(Store.id).first()
                if first_store:
                    session['active_store_id'] = first_store.id
            elif user.role == 'staff' and user.store_id:
                session['active_store_id'] = user.store_id
            # super_admin はテナント管理へ、それ以外は売上管理ダッシュボードへ
            if user.role == 'super_admin':
                dashboard_url = url_for('admin_tenants')
            elif user.role == 'staff':
                # 18: スタッフは自分のデータがデフォルト表示される顧客管理表へ
                dashboard_url = url_for('customer_management')
            else:
                dashboard_url = url_for('executive_dashboard')
            return make_response(f'''<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>ログイン中...</title></head><body>
<script>
  sessionStorage.setItem("rp_auth","1");
  window.location.replace("{dashboard_url}");
</script>
</body></html>''')
        else:
            error = "ユーザー名またはパスワードが正しくありません。"
    return render_template("app_login.html", error=error)


@app.route("/app-logout")
def app_logout():
    """管理ツールログアウト"""
    session.pop('app_user_id', None)
    session.pop('app_user_role', None)
    session.pop('app_username', None)
    return redirect(url_for('app_login'))


# ── 幹部向け管理ツール：ページルート ─────────────────────

@app.route("/executive")
@login_required
@manager_or_above_required
def executive_dashboard():
    """売上管理ダッシュボード"""
    stores = get_allowed_stores(ignore_active=True)  # サイドバー用
    active_ids = get_allowed_store_ids()  # アクティブ店舗のみ
    allowed_ids = [s.id for s in stores]  # サイドバー用全店舗
    staff_list = Staff.query.filter(Staff.store_id.in_(active_ids), Staff.is_active == True).all()
    year, month = current_ym()
    store_id = active_ids[0] if active_ids else None
    return render_template("executive_dashboard.html",
                           stores=stores, staff_list=staff_list, year=year, month=month,
                           store_id=store_id, now=datetime.now())


@app.route("/sales")
@login_required
@block_super_admin
def sales_management():
    """営業管理ページ：KPI入力・閲覧"""
    stores = get_allowed_stores()
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    year, month = current_ym()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    cur_role = cur_user.role if cur_user else 'staff'
    cur_staff_id = cur_user.staff_id if cur_user else None
    is_manager = cur_role in ('owner', 'store_manager', 'super_admin')
    store_id = allowed_ids[0] if allowed_ids else None
    media_types = MediaType.query.filter_by(store_id=store_id, is_active=True).order_by(MediaType.sort_order, MediaType.name).all() if store_id else []
    return render_template("sales_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now(),
                           cur_role=cur_role, cur_staff_id=cur_staff_id,
                           is_manager=is_manager, media_types=media_types,
                           store_id=store_id)


@app.route("/customer-management")
@login_required
@block_super_admin
def customer_management():
    """顧客管理表ページ：申込・入金管理に特化"""
    stores = get_allowed_stores(ignore_active=True)   # サイドバー用（全店舗）
    active_ids = get_allowed_store_ids()              # アクティブ店舗のみ
    staff_list = Staff.query.filter(Staff.store_id.in_(active_ids), Staff.is_active == True).all() if active_ids else []
    year, month = current_ym()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    cur_role = cur_user.role if cur_user else 'staff'
    cur_staff_id = cur_user.staff_id if cur_user else None
    is_manager = cur_role in ('owner', 'store_manager', 'super_admin')
    store_id = active_ids[0] if active_ids else None
    media_types = MediaType.query.filter_by(store_id=store_id, is_active=True).order_by(MediaType.sort_order, MediaType.name).all() if store_id else []
    return render_template("customer_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now(),
                           cur_role=cur_role, cur_staff_id=cur_staff_id,
                           is_manager=is_manager, media_types=media_types,
                           store_id=store_id)


@app.route("/contract-customers")
@login_required
@block_super_admin
def contract_customers():
    """契約中顧客管理ページ：審査〇で契約に進んだ顧客の契約書類を準備する"""
    stores = get_allowed_stores(ignore_active=True)
    active_ids = get_allowed_store_ids()
    staff_list = Staff.query.filter(Staff.store_id.in_(active_ids), Staff.is_active == True).all() if active_ids else []
    cur_user = AppUser.query.get(session.get('app_user_id'))
    cur_role = cur_user.role if cur_user else 'staff'
    is_manager = cur_role in ('owner', 'store_manager', 'super_admin')
    store_id = active_ids[0] if active_ids else None
    return render_template("contract_customers.html", stores=stores, staff_list=staff_list,
                           now=datetime.now(), cur_role=cur_role,
                           is_manager=is_manager, store_id=store_id)


@app.route("/api/contract-customers")
@login_required
def api_contract_customers():
    """契約中顧客一覧：審査〇（review_status='ok'）になった顧客（＝契約に進んだ顧客）"""
    allowed_ids = get_allowed_store_ids()
    staff_id = request.args.get('staff_id', type=int)
    q = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ApplicationRecord.review_status == 'ok',
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
    )
    if staff_id:
        q = q.filter(ApplicationRecord.staff_id == staff_id)
    recs = q.order_by(ApplicationRecord.application_date.asc(), ApplicationRecord.id.asc()).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify([_app_record_to_dict(r, staff_map) for r in recs])


def _contract_doc_defaults(rec, staff):
    """申込データから契約書類（取引成立台帳）の初期値を作る"""
    fd = lambda d: d.isoformat() if d else ''
    return {
        'torihiki': '媒介',
        'keiyaku_date': fd(rec.contract_start_date),
        'hikiwatashi_date': fd(rec.contract_start_date),
        'kashinushi_name': '', 'kashinushi_addr': '', 'kashinushi_tel': '',
        'karinushi_name': rec.customer_name or '', 'karinushi_addr': '', 'karinushi_tel': '',
        'bukken_shozai': '',
        'kouzou': '', 'yane': '', 'youto': '居宅',
        'kaisuu_above': '', 'kaisuu_below': '', 'menseki': '', 'madori': '',
        'meisho': rec.property_name or '', 'goushitsu': rec.room_number or '',
        'setsubi': [], 'fuzoku': [], 'sonota': '',
        'chinryo': int(rec.rent or 0), 'kanrihi': '', 'chuusha': '',
        'reikin': '', 'shikikin': '', 'hosho': '', 'kazai_hoken': '',
        'cleaning': '', 'support24': '', 'chonaikai': '', 'catv': '', 'suidou': '',
        'keiyaku_shurui': '普通借家',
        'kanri_kaisha': rec.management_company or '',
        'tantou': staff.name if staff else '',
        'biko': '',
    }


@app.route("/contract-customers/<int:rid>/edit")
@login_required
@block_super_admin
def contract_document_edit(rid):
    """契約書類エディタページ"""
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return "権限がありません", 403
    return render_template("contract_editor.html", rid=rid,
                           customer_name=rec.customer_name or '',
                           property_name=rec.property_name or '')


@app.route("/api/contract-customers/<int:rid>/document-data", methods=["GET"])
@login_required
def api_contract_document_get(rid):
    """契約書類の保存済み編集データと、申込からの初期値コンテキストを返す"""
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    staff = Staff.query.get(rec.staff_id) if rec.staff_id else None
    store = Store.query.get(rec.store_id)
    fd = lambda d: d.isoformat() if d else ''
    ad_yen = round((rec.rent or 0) * (rec.ad_amount or 0) / 100) if (rec.ad_type or 'amount') == 'percent' else (rec.ad_amount or 0)
    ctx = {
        'customer_name': rec.customer_name or '',
        'property_name': rec.property_name or '',
        'room_number': rec.room_number or '',
        'rent': int(rec.rent or 0),
        'management_company': rec.management_company or '',
        'staff_name': staff.name if staff else '',
        'store_name': store.name if store else '',
        'contract_start_date': fd(rec.contract_start_date),
        'application_date': fd(rec.application_date),
        'brokerage_fee': int(rec.brokerage_fee or 0),
        'option_amount': int(rec.option_amount or 0),
        'ad_yen': int(ad_yen),
        'media': rec.media or '',
    }
    data = {}
    doc = ContractDocument.query.filter_by(application_id=rid).first()
    if doc and doc.data:
        try:
            saved = json.loads(doc.data)
            if isinstance(saved, dict):
                data = saved
        except Exception:
            pass
    return jsonify({'data': data, 'ctx': ctx, 'saved': bool(doc)})


@app.route("/api/contract-customers/<int:rid>/document-data", methods=["POST"])
@login_required
def api_contract_document_save(rid):
    """契約書類の編集データを保存"""
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if cur_user and cur_user.role == 'staff' and rec.staff_id != cur_user.staff_id:
        return jsonify({'error': '権限がありません'}), 403
    payload = request.get_json() or {}
    doc = ContractDocument.query.filter_by(application_id=rid).first()
    if not doc:
        doc = ContractDocument(application_id=rid, store_id=rec.store_id)
        db.session.add(doc)
    doc.data = json.dumps(payload, ensure_ascii=False)
    doc.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── 間取り作成 ──────────────────────────────────────────
@app.route("/floorplan")
@login_required
@block_super_admin
def floorplan_page():
    """間取り作成ページ"""
    return render_template("floorplan.html")


@app.route("/api/floorplans", methods=["GET"])
@login_required
def api_floorplans_list():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    q = FloorPlan.query
    if sid:
        q = q.filter(FloorPlan.store_id == sid)
    items = q.order_by(FloorPlan.updated_at.desc()).all()
    return jsonify([{'id': f.id, 'name': f.name or '無題',
                     'updated_at': f.updated_at.strftime('%Y-%m-%d %H:%M') if f.updated_at else ''}
                    for f in items])


@app.route("/api/floorplans", methods=["POST"])
@login_required
def api_floorplans_create():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    data = request.get_json() or {}
    fp_id = data.get('id')
    fp = FloorPlan.query.get(fp_id) if fp_id else None
    if fp:
        if fp.store_id != sid:
            return jsonify({'error': '権限がありません'}), 403
    else:
        fp = FloorPlan(store_id=sid)
        db.session.add(fp)
    fp.name = (data.get('name') or '無題')[:200]
    fp.data = data.get('data') or ''
    fp.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok', 'id': fp.id})


@app.route("/api/floorplans/<int:fid>", methods=["GET"])
@login_required
def api_floorplans_get(fid):
    allowed = get_allowed_store_ids()
    fp = FloorPlan.query.get_or_404(fid)
    if fp.store_id not in allowed:
        return jsonify({'error': '権限がありません'}), 403
    return jsonify({'id': fp.id, 'name': fp.name, 'data': fp.data or ''})


@app.route("/api/floorplans/<int:fid>", methods=["DELETE"])
@login_required
def api_floorplans_delete(fid):
    allowed = get_allowed_store_ids()
    fp = FloorPlan.query.get_or_404(fid)
    if fp.store_id not in allowed:
        return jsonify({'error': '権限がありません'}), 403
    db.session.delete(fp)
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── 反響メール自動取込（IMAP） ─────────────────────────────

# ① 差出人ドメイン/アドレス → 媒体名（部分一致）
PORTAL_DOMAIN_MAP = [
    ('smocca',   'スモッカ'),
    ('suumo',    'SUUMO'),
    ('recruit',  'SUUMO'),
    ('homes',    "HOME'S"),
    ('lifull',   "HOME'S"),
    ('athome',   'アットホーム'),
    ('at-home',  'アットホーム'),
    ('chintai',  'CHINTAI'),
    ('apaman',   'アパマンショップ'),
    ('canary',   'カナリー'),
    ('cnary',    'カナリー'),
    ('ielove',   'いえらぶ'),
    ('eheya',    'エイブル'),
    ('pittat',   'ピタットハウス'),
]

# ② 差出人名/件名/本文に含まれるキーワード → 媒体名（ドメインで判定できない場合）
PORTAL_KEYWORD_MAP = [
    ('スモッカ',     'スモッカ'),
    ('SUUMO',        'SUUMO'),
    ('スーモ',       'SUUMO'),
    ("HOME'S",       "HOME'S"),
    ('ホームズ',     "HOME'S"),
    ('アットホーム', 'アットホーム'),
    ('カナリー',     'カナリー'),
    ('CHINTAI',      'CHINTAI'),
    ('アパマン',     'アパマンショップ'),
    ('いえらぶ',     'いえらぶ'),
    ('エイブル',     'エイブル'),
    ('ピタット',     'ピタットハウス'),
]

# 項目ラベルの同義語 → 正規キー（媒体差を吸収）
_FIELD_SYNONYMS = {
    'name':      ['氏名(漢字)', '氏名（漢字）', '氏名', 'お名前', '名前', 'ご氏名', 'お客様名', 'お客さま名'],
    'phone':     ['電話番号', 'tel', '電話', 'お電話番号', '携帯電話', '携帯番号', '連絡先電話番号', 'ご連絡先'],
    'email':     ['メールアドレス', 'email', 'e-mail', 'mail', 'メール', 'eメール'],
    'property':  ['物件名', '建物名', 'マンション名'],
    'room':      ['部屋番号', '号室'],
    'rent':      ['賃料', '家賃'],
    'address':   ['住所', '所在地', '物件所在地'],
    'madori':    ['間取り', '間取'],
    'menseki':   ['面積', '専有面積'],
    'station':   ['最寄駅', '最寄り駅'],
    'datetime':  ['お問合わせ日時', 'お問い合わせ日時', 'お問合せ日時', '問合せ日時', '問い合わせ日時', '受付日時', '反響日時', '受信日時'],
    'extid':     ['スモッカ反響id', '反響id', '問い合わせ番号', 'お問い合わせ番号', '受付番号', '反響番号', '問合せ番号'],
    'inquiry':   ['お問合せ内容', 'お問い合わせ内容', '物件に関するお問合せ内容', 'お問合せ', 'ご要望', '希望内容'],
    'bukken_no': ['物件管理番号', '物件番号', '管理番号'],
    'note':      ['備考', 'その他', 'ご質問', 'メッセージ', 'コメント'],
}


def _norm_label(s):
    return (s or '').replace(' ', '').replace('　', '').strip().lower()


# 正規化済みラベル → キー
_LABEL_TO_KEY = {}
for _k, _syns in _FIELD_SYNONYMS.items():
    for _s in _syns:
        _LABEL_TO_KEY[_norm_label(_s)] = _k


def _decode_mime(s):
    if not s:
        return ''
    try:
        out = ''
        for txt, enc in _decode_header(s):
            if isinstance(txt, bytes):
                out += txt.decode(enc or 'utf-8', errors='replace')
            else:
                out += txt
        return out
    except Exception:
        return str(s)


def _email_plain_body(msg):
    """メールから本文(プレーンテキスト)を取得"""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition') or ''):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        body += payload.decode(charset, errors='replace')
                    except Exception:
                        body += payload.decode('utf-8', errors='replace')
        if not body:
            for part in msg.walk():
                if part.get_content_type() == 'text/html':
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body += re.sub(r'<[^>]+>', ' ', payload.decode(charset, errors='replace'))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            body = payload.decode(charset, errors='replace')
    return body


def parse_custom_keywords(text):
    """設定の追加キーワード文字列を [(キーワード, 媒体名), ...] に変換。
    1行＝「語」または「語=媒体名」「語,媒体名」「語：媒体名」。"""
    rules = []
    for raw in (text or '').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            continue
        m = re.split(r'[=＝,，：:]', line, 1)
        kw = m[0].strip()
        media = m[1].strip() if len(m) > 1 and m[1].strip() else kw
        if kw:
            rules.append((kw, media))
    return rules


def _detect_source(from_addr, subject='', body='', extra_map=None):
    """媒体名を ①ドメイン → ②追加キーワード → ③標準キーワード の順で判定。不明なら None。"""
    a = (from_addr or '').lower()
    for key, name in PORTAL_DOMAIN_MAP:
        if key in a:
            return name
    hay = f"{from_addr}\n{subject}\n{body[:800]}".upper()
    for key, name in (extra_map or []):
        if key and key.upper() in hay:
            return name
    for key, name in PORTAL_KEYWORD_MAP:
        if key.upper() in hay:
            return name
    return None


def _from_display_name(from_addr):
    """差出人名（または ドメイン名）を媒体名のフォールバックとして返す"""
    s = from_addr or ''
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', s)
    if m:
        nm = m.group(1).strip()
        if nm and '@' not in nm:
            return nm[:40]
    md = re.search(r'@([\w.-]+)', s)
    if md:
        parts = md.group(1).split('.')
        if len(parts) >= 2:
            return parts[-2]
    return 'その他反響'


def parse_reaction_email(msg, extra_map=None):
    """反響メールを解析。反響メールでなければ None を返す。
    未知の媒体でも、氏名/連絡先＋物件などの構造があれば取り込む。"""
    from_addr = _decode_mime(msg.get('From', ''))
    subject = _decode_mime(msg.get('Subject', ''))
    body = _email_plain_body(msg)
    if not body:
        return None

    fields = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or ('：' not in line and ':' not in line):
            continue
        parts = re.split(r'[：:]', line, 1)
        if len(parts) != 2:
            continue
        label = _norm_label(parts[0])
        value = parts[1].strip()
        if not label or value in ('', '－', '-', 'ー', '−'):
            continue
        key = _LABEL_TO_KEY.get(label)
        if key and key not in fields:
            fields[key] = value

    name = fields.get('name', '')

    # 反響メール判定：連絡先＋物件情報の両方が取れていれば反響とみなす（未知媒体も対象）
    contact = name or fields.get('phone') or fields.get('email')
    prop = (fields.get('property') or fields.get('extid')
            or fields.get('bukken_no') or fields.get('inquiry'))
    if not (contact and prop):
        return None
    source = _detect_source(from_addr, subject, body, extra_map) or _from_display_name(from_addr)

    # 反響日時
    edate = None
    dt_raw = fields.get('datetime', '')
    mdt = re.search(r'(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})', dt_raw)
    if mdt:
        try:
            edate = date(int(mdt.group(1)), int(mdt.group(2)), int(mdt.group(3)))
        except Exception:
            edate = None
    if edate is None:
        try:
            dh = emaillib.utils.parsedate_to_datetime(msg.get('Date'))
            if dh:
                edate = dh.date()
        except Exception:
            edate = None
    if edate is None:
        edate = date.today()

    # 一意ID（重複取込防止）
    raw_id = fields.get('extid', '')
    if raw_id:
        ext = f"{source}:{raw_id}"
    else:
        basis = f"{source}|{name}|{fields.get('property','')}|{fields.get('bukken_no','')}|{dt_raw}"
        ext = 'h:' + hashlib.md5(basis.encode('utf-8')).hexdigest()[:20]

    # 自由記述（お問合せ内容・備考）も拾う
    def _grab(markers):
        for mk in markers:
            i = body.find(mk)
            if i < 0:
                continue
            collected = []
            for ln in body[i + len(mk):].splitlines():
                s = ln.strip().lstrip('・').strip()
                if not s:
                    if collected:
                        break
                    continue
                # 区切り線/セクション見出し/ラベル行で終了
                if s.startswith('＜') or s.startswith('■') or set(s) <= set('-—─=＝_＿ 　'):
                    if collected:
                        break
                    continue
                if '：' in s or ':' in s:
                    if collected:
                        break
                    continue
                collected.append(s)
                if len(collected) >= 4:
                    break
            if collected:
                return ' '.join(collected)[:300]
        return ''

    inquiry_free = fields.get('inquiry') or _grab(['＜物件に関するお問合せ内容＞', '物件に関するお問合せ内容', '＜お問合せ内容＞', 'お問合せ内容'])
    biko_free = fields.get('note') or _grab(['■備考', '＜問い合わせ詳細＞', '備考'])

    # メモ生成（拾えた項目のみ）
    memo_lines = []
    for lbl, key in [('物件', 'property'), ('部屋番号', 'room'), ('賃料', 'rent'),
                     ('間取り', 'madori'), ('面積', 'menseki'), ('最寄駅', 'station'),
                     ('住所', 'address'), ('物件管理番号', 'bukken_no'),
                     ('電話', 'phone'), ('メール', 'email')]:
        v = fields.get(key)
        if v:
            memo_lines.append(f"{lbl}：{v}")
    if inquiry_free:
        memo_lines.append(f"お問合せ内容：{inquiry_free}")
    if biko_free:
        memo_lines.append(f"備考：{biko_free}")
    if raw_id:
        memo_lines.append(f"反響ID：{raw_id}")

    return {
        'source': source,
        'name': name or '（氏名不明）',
        'date': edate,
        'memo': '\n'.join(memo_lines),
        'external_id': ext,
        'has_phone': bool(fields.get('phone')),
    }


def test_imap_connection(host, user, password):
    try:
        M = imaplib.IMAP4_SSL(host or 'imap.gmail.com')
        M.login(user, password)
        M.select('INBOX')
        try:
            M.logout()
        except Exception:
            pass
        return True, '接続に成功しました'
    except imaplib.IMAP4.error as e:
        return False, f'ログインに失敗しました（メールアドレス／アプリパスワードをご確認ください）: {e}'
    except Exception as e:
        return False, f'接続エラー：{e}'


def fetch_reactions_for_store(store_id, limit=120, since_days=30):
    """指定店舗のGmailから反響メールを取得し EchoRecord へ登録"""
    ms = MailSetting.query.filter_by(store_id=store_id).first()
    if not ms or not ms.imap_user or not ms.imap_pass:
        return {'ok': False, 'error': '未設定', 'imported': 0, 'scanned': 0}
    extra_map = parse_custom_keywords(ms.custom_keywords)
    imported = 0
    scanned = 0
    try:
        M = imaplib.IMAP4_SSL(ms.imap_host or 'imap.gmail.com')
        M.login(ms.imap_user, ms.imap_pass)
        M.select('INBOX')
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        since = date.today() - timedelta(days=since_days)
        since_str = f"{since.day:02d}-{months[since.month - 1]}-{since.year}"
        typ, data = M.search(None, f'(SINCE "{since_str}")')
        ids = data[0].split() if (data and data[0]) else []
        ids = ids[-limit:]
        for num in reversed(ids):
            try:
                typ, md = M.fetch(num, '(BODY.PEEK[])')
                if not md or not md[0]:
                    continue
                msg = emaillib.message_from_bytes(md[0][1])
            except Exception:
                continue
            scanned += 1
            parsed = parse_reaction_email(msg, extra_map)
            if not parsed:
                continue
            exists = EchoRecord.query.filter_by(store_id=store_id, external_id=parsed['external_id']).first()
            if exists:
                continue
            db.session.add(EchoRecord(
                store_id=store_id,
                staff_id=ms.default_staff_id or None,
                list_name=parsed['name'],
                echo_date=parsed['date'],
                media=parsed['source'],
                method='メール',
                memo=parsed['memo'],
                has_phone=parsed['has_phone'],
                external_id=parsed['external_id'],
            ))
            imported += 1
        db.session.commit()
        try:
            M.close()
            M.logout()
        except Exception:
            pass
        ms.last_fetch_at = datetime.utcnow()
        ms.last_result = f"取得OK：{imported}件追加 / {scanned}件確認（{datetime.now():%m/%d %H:%M}）"
        db.session.commit()
        return {'ok': True, 'imported': imported, 'scanned': scanned}
    except imaplib.IMAP4.error as e:
        db.session.rollback()
        try:
            ms.last_result = f"ログイン失敗：{e}"
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'ok': False, 'error': f'ログインに失敗しました（メールアドレス／アプリパスワードをご確認ください）', 'imported': imported, 'scanned': scanned}
    except Exception as e:
        db.session.rollback()
        try:
            ms.last_result = f"エラー：{e}"
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'ok': False, 'error': str(e), 'imported': imported, 'scanned': scanned}


# ── 自動取込サービス：IMAP IDLE（リアルタイム push） ──
# gunicorn複数ワーカーでもソケットlockで1プロセスのみがIDLE接続を保持する。
_MAIL_SERVICE_STARTED = False
_IS_LEADER = False


def _imap_idle_wait(M, timeout, stop_event):
    """IMAP IDLE で新着を待つ。新着通知→True / タイムアウト→False。"""
    tag = M._new_tag()
    M.send(tag + b' IDLE\r\n')
    M.readline()  # '+ idling'
    got = False
    sock = M.socket()
    old_to = sock.gettimeout()
    end = time.time() + timeout
    try:
        sock.settimeout(5)
        while not stop_event.is_set() and time.time() < end:
            try:
                line = M.readline()
            except socket.timeout:
                continue
            except Exception:
                break
            if not line:
                break
            u = line.upper()
            if b'EXISTS' in u or b'RECENT' in u:
                got = True
                break
    finally:
        try:
            sock.settimeout(old_to)
        except Exception:
            pass
        try:
            M.send(b'DONE\r\n')
            for _ in range(10):
                l = M.readline()
                if not l or l.upper().startswith(tag.upper()):
                    break
        except Exception:
            pass
    return got


def _idle_worker(store_id, stop_event):
    """1店舗ぶんのIMAP接続を保持し、新着が来たら即フェッチ。切断時は自動再接続。"""
    backoff = 5
    while not stop_event.is_set():
        M = None
        try:
            with app.app_context():
                ms = MailSetting.query.filter_by(store_id=store_id).first()
                if not ms or not ms.enabled or not ms.imap_user or not ms.imap_pass:
                    return
                host, user, pw = (ms.imap_host or 'imap.gmail.com'), ms.imap_user, ms.imap_pass
            M = imaplib.IMAP4_SSL(host)
            M.login(user, pw)
            M.select('INBOX')
            with app.app_context():   # 接続直後にキャッチアップ
                fetch_reactions_for_store(store_id)
            backoff = 5
            has_idle = b'IDLE' in (M.capabilities or ())
            while not stop_event.is_set():
                if has_idle:
                    _imap_idle_wait(M, 1500, stop_event)   # 最大25分でIDLE更新
                else:
                    stop_event.wait(60)                    # IDLE非対応→60秒間隔
                if stop_event.is_set():
                    break
                with app.app_context():
                    fetch_reactions_for_store(store_id)
        except Exception as e:
            print(f"idle worker store={store_id} reconnect: {e}")
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)
        finally:
            try:
                if M:
                    M.logout()
            except Exception:
                pass


class _IdleManager:
    def __init__(self):
        self.workers = {}   # store_id -> {'thread','stop','sig'}
        self.lock = threading.Lock()

    @staticmethod
    def _sig(ms):
        return ((ms.imap_host or 'imap.gmail.com'), ms.imap_user, ms.imap_pass)

    def sync(self):
        """有効店舗のIDLEワーカーを起動／無効・変更・停止したものを停止。"""
        try:
            with app.app_context():
                enabled = {ms.store_id: self._sig(ms)
                           for ms in MailSetting.query.filter_by(enabled=True).all()
                           if ms.imap_user and ms.imap_pass}
        except Exception as e:
            print(f"idle sync query error: {e}")
            return
        with self.lock:
            for sid in list(self.workers.keys()):
                w = self.workers[sid]
                if sid not in enabled or enabled[sid] != w['sig'] or not w['thread'].is_alive():
                    w['stop'].set()
                    del self.workers[sid]
            for sid, sig in enabled.items():
                if sid not in self.workers:
                    ev = threading.Event()
                    t = threading.Thread(target=_idle_worker, args=(sid, ev), daemon=True)
                    self.workers[sid] = {'thread': t, 'stop': ev, 'sig': sig}
                    t.start()
                    print(f"idle worker started store={sid}")


idle_manager = _IdleManager()


def _mail_sync_loop():
    time.sleep(20)
    cnt = 0
    while True:
        try:
            idle_manager.sync()
            cnt += 1
            if cnt % 20 == 0:   # 約10分ごとに保険フェッチ（IDLE取りこぼし対策）
                with app.app_context():
                    for ms in MailSetting.query.filter_by(enabled=True).all():
                        if ms.imap_user and ms.imap_pass:
                            try:
                                fetch_reactions_for_store(ms.store_id)
                            except Exception:
                                pass
        except Exception as e:
            print(f"mail sync loop error: {e}")
        time.sleep(30)


def start_mail_service():
    """ソケットlockでリーダーを1プロセスだけ選出し、IDLEサービスを開始。"""
    global _MAIL_SERVICE_STARTED, _IS_LEADER
    if _MAIL_SERVICE_STARTED:
        return
    try:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock.bind(('127.0.0.1', 47625))
        globals()['_MAIL_LOCK'] = lock   # GC防止のため保持
    except OSError:
        _MAIL_SERVICE_STARTED = True      # 非リーダーは以後起動しない
        return
    _MAIL_SERVICE_STARTED = True
    _IS_LEADER = True
    threading.Thread(target=_mail_sync_loop, daemon=True).start()
    print("mail IDLE service started (leader)")


def request_mail_sync():
    """設定保存時などに即時で有効店舗のワーカーを同期（リーダーのみ実行）。"""
    if _IS_LEADER:
        threading.Thread(target=idle_manager.sync, daemon=True).start()


# ── 反響メール取込 設定ページ・API ──
@app.route("/mail-settings")
@login_required
@block_super_admin
def mail_settings_page():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    staff_list = Staff.query.filter(Staff.store_id == sid, Staff.is_active == True).all() if sid else []
    return render_template("mail_settings.html", staff_list=staff_list)


@app.route("/api/mail-settings", methods=["GET"])
@login_required
def api_mail_settings_get():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    ms = MailSetting.query.filter_by(store_id=sid).first() if sid else None
    if not ms:
        return jsonify({'imap_user': '', 'imap_host': 'imap.gmail.com', 'enabled': False,
                        'default_staff_id': None, 'has_password': False, 'custom_keywords': '',
                        'last_result': '', 'last_fetch_at': None})
    return jsonify({
        'imap_user': ms.imap_user or '',
        'imap_host': ms.imap_host or 'imap.gmail.com',
        'enabled': bool(ms.enabled),
        'default_staff_id': ms.default_staff_id,
        'has_password': bool(ms.imap_pass),
        'custom_keywords': ms.custom_keywords or '',
        'last_result': ms.last_result or '',
        'last_fetch_at': ms.last_fetch_at.strftime('%Y-%m-%d %H:%M') if ms.last_fetch_at else None,
    })


@app.route("/api/mail-settings", methods=["POST"])
@login_required
def api_mail_settings_save():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    if not sid:
        return jsonify({'error': 'unauthorized'}), 403
    data = request.get_json() or {}
    ms = MailSetting.query.filter_by(store_id=sid).first()
    if not ms:
        ms = MailSetting(store_id=sid)
        db.session.add(ms)
    ms.imap_user = (data.get('imap_user') or '').strip()[:200]
    ms.imap_host = ((data.get('imap_host') or '').strip() or 'imap.gmail.com')[:120]
    pw = data.get('imap_pass')
    if pw:  # 入力があった時のみ更新（空欄なら既存パスワードを維持）
        ms.imap_pass = pw.replace(' ', '').strip()[:200]
    ms.enabled = bool(data.get('enabled'))
    dsid = data.get('default_staff_id')
    ms.default_staff_id = int(dsid) if dsid else None
    ms.custom_keywords = (data.get('custom_keywords') or '')[:5000]
    ms.updated_at = datetime.utcnow()
    db.session.commit()
    start_mail_service()   # 未起動なら起動
    request_mail_sync()    # 設定変更を即反映（リーダーのみ）
    return jsonify({'status': 'ok'})


@app.route("/api/mail-settings/test", methods=["POST"])
@login_required
def api_mail_settings_test():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    data = request.get_json() or {}
    user = (data.get('imap_user') or '').strip()
    host = (data.get('imap_host') or 'imap.gmail.com').strip()
    pw = (data.get('imap_pass') or '')
    if not pw:  # 未入力なら保存済みパスワードでテスト
        ms = MailSetting.query.filter_by(store_id=sid).first() if sid else None
        pw = ms.imap_pass if ms else ''
    pw = (pw or '').replace(' ', '').strip()
    if not user or not pw:
        return jsonify({'ok': False, 'message': 'メールアドレスとアプリパスワードを入力してください'})
    ok, msg = test_imap_connection(host, user, pw)
    return jsonify({'ok': ok, 'message': msg})


@app.route("/api/mail-settings/fetch", methods=["POST"])
@login_required
def api_mail_settings_fetch():
    allowed = get_allowed_store_ids()
    sid = allowed[0] if allowed else None
    if not sid:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403
    res = fetch_reactions_for_store(sid)
    return jsonify(res)


@app.route("/echo-management")
@login_required
@block_super_admin
def echo_management():
    """反響管理表ページ"""
    stores = get_allowed_stores(ignore_active=True)  # サイドバー用
    active_ids = get_allowed_store_ids()  # アクティブ店舗のみ
    allowed_ids = [s.id for s in stores]  # サイドバー用全店舗
    staff_list = Staff.query.filter(Staff.store_id.in_(active_ids), Staff.is_active == True).all() if active_ids else []
    year, month = current_ym()
    store_id = active_ids[0] if active_ids else None
    return render_template("echo_management.html",
                           stores=stores, staff_list=staff_list,
                           year=year, month=month, store_id=store_id,
                           now=datetime.now())


@app.route("/api/echo-records", methods=["GET"])
@login_required
def api_echo_records_list():
    allowed = get_allowed_store_ids()
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    staff_id = request.args.get('staff_id', type=int)

    from datetime import date as _date
    m_start = _date(year, month, 1)
    m_end   = _date(year + (month // 12), (month % 12) + 1, 1)

    q = EchoRecord.query.filter(
        EchoRecord.store_id.in_(allowed),
        EchoRecord.echo_date >= m_start,
        EchoRecord.echo_date <  m_end,
    )
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    records = q.order_by(EchoRecord.echo_date.asc(), EchoRecord.id.asc()).all()

    def fd(d): return d.strftime('%Y-%m-%d') if d else None
    def sname(sid): s = Staff.query.get(sid); return s.name if s else ''
    return jsonify([{
        'id': r.id, 'store_id': r.store_id, 'staff_id': r.staff_id,
        'staff_name': sname(r.staff_id),
        'list_name': r.list_name or '', 'echo_date': fd(r.echo_date),
        'media': r.media or '', 'method': r.method or '',
        'first_contact_date': fd(r.first_contact_date),
        **{f'followup_{i}': fd(getattr(r, f'followup_{i}')) for i in range(1, 11)},
        'has_reply': r.has_reply, 'has_phone': r.has_phone, 'has_line': r.has_line,
        'memo': r.memo or '',
    } for r in records])


@app.route("/api/echo-records", methods=["POST"])
@login_required
def api_echo_records_add():
    data = request.get_json() or {}
    allowed = get_allowed_store_ids()
    if not allowed:
        return jsonify({'error': 'unauthorized'}), 403
    from datetime import datetime as _dt
    def pd(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    sid = int(data.get('store_id') or 0) or allowed[0]
    if sid not in allowed:
        sid = allowed[0]
    r = EchoRecord(
        store_id=sid,
        staff_id=int(data.get('staff_id') or 0) or None,
        list_name=data.get('list_name', ''),
        echo_date=pd(data.get('echo_date')),
        media=data.get('media', ''),
        method=data.get('method', ''),
        first_contact_date=pd(data.get('first_contact_date')),
        **{f'followup_{i}': pd(data.get(f'followup_{i}')) for i in range(1, 11)},
        has_reply=bool(data.get('has_reply')),
        has_phone=bool(data.get('has_phone')),
        has_line=bool(data.get('has_line')),
        memo=data.get('memo', ''),
    )
    db.session.add(r)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': r.id})


@app.route("/api/echo-records/<int:rid>", methods=["PUT"])
@login_required
def api_echo_records_update(rid):
    r = EchoRecord.query.get_or_404(rid)
    data = request.get_json() or {}
    from datetime import datetime as _dt
    def pd(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    # 部分更新対応：payload に含まれるキーのみ更新（インライン編集で他項目を消さない）
    if 'staff_id' in data:
        r.staff_id = int(data.get('staff_id') or 0) or None
    if 'list_name' in data:
        r.list_name = data.get('list_name')
    if 'echo_date' in data:
        r.echo_date = pd(data.get('echo_date')) or r.echo_date
    if 'media' in data:
        r.media = data.get('media')
    if 'method' in data:
        r.method = data.get('method')
    if 'first_contact_date' in data:
        r.first_contact_date = pd(data.get('first_contact_date'))
    for i in range(1, 11):
        key = f'followup_{i}'
        if key in data:
            setattr(r, key, pd(data.get(key)))
    if 'has_reply' in data:
        r.has_reply = bool(data.get('has_reply'))
    if 'has_phone' in data:
        r.has_phone = bool(data.get('has_phone'))
    if 'has_line' in data:
        r.has_line = bool(data.get('has_line'))
    if 'memo' in data:
        r.memo = data.get('memo')
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/echo-records/<int:rid>", methods=["DELETE"])
@login_required
def api_echo_records_delete(rid):
    r = EchoRecord.query.get_or_404(rid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/customer-service")
@login_required
@block_super_admin
def customer_service():
    """接客管理表ページ"""
    stores = get_allowed_stores(ignore_active=True)  # サイドバー用
    active_ids = get_allowed_store_ids()  # アクティブ店舗のみ
    allowed_ids = [s.id for s in stores]  # サイドバー用全店舗
    staff_list = Staff.query.filter(Staff.store_id.in_(active_ids), Staff.is_active == True).all() if active_ids else []
    year, month = current_ym()
    store_id = active_ids[0] if active_ids else None
    return render_template("customer_service.html",
                           stores=stores, staff_list=staff_list,
                           year=year, month=month, store_id=store_id,
                           now=datetime.now())


# ─── DropdownOption API ──────────────────────────────────────────────────

def _get_tenant_id():
    """現在ログイン中ユーザーのtenant_idを返す"""
    uid = session.get('app_user_id')
    if not uid:
        return None
    u = AppUser.query.get(uid)
    return u.tenant_id if u else None


@app.route("/api/dropdown/<category>", methods=["GET"])
@login_required
def api_dropdown_get(category):
    """カテゴリのプルダウン選択肢を返す（テナント固有 + 共通デフォルト）"""
    tenant_id = _get_tenant_id()
    opts = DropdownOption.query.filter(
        DropdownOption.category == category,
        db.or_(DropdownOption.tenant_id == tenant_id, DropdownOption.tenant_id == None)
    ).order_by(DropdownOption.sort_order, DropdownOption.id).all()
    return jsonify([{'id': o.id, 'value': o.value} for o in opts])


@app.route("/api/dropdown/<category>", methods=["POST"])
@login_required
def api_dropdown_add(category):
    """選択肢を追加する"""
    data = request.get_json() or {}
    value = (data.get('value') or '').strip()
    if not value:
        return jsonify({'error': 'value required'}), 400
    tenant_id = _get_tenant_id()
    max_order = db.session.query(db.func.max(DropdownOption.sort_order)).filter_by(category=category).scalar() or 0
    opt = DropdownOption(tenant_id=tenant_id, category=category, value=value, sort_order=max_order + 1)
    db.session.add(opt)
    db.session.commit()
    return jsonify({'id': opt.id, 'value': opt.value})


@app.route("/api/dropdown/item/<int:option_id>", methods=["DELETE"])
@login_required
def api_dropdown_delete(option_id):
    """選択肢を削除する"""
    opt = DropdownOption.query.get_or_404(option_id)
    db.session.delete(opt)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────────────────────────────────

@app.route("/api/customer-service-records", methods=["GET"])
@login_required
def api_cs_records_list():
    allowed = get_allowed_store_ids()
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    staff_id = request.args.get('staff_id', type=int)

    from datetime import date as _date
    m_start = _date(year, month, 1)
    m_end   = _date(year + (month // 12), (month % 12) + 1, 1)

    q = CustomerServiceRecord.query.filter(
        CustomerServiceRecord.store_id.in_(allowed),
        CustomerServiceRecord.service_date >= m_start,
        CustomerServiceRecord.service_date <  m_end,
    )
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    records = q.order_by(CustomerServiceRecord.service_date.asc(), CustomerServiceRecord.id.asc()).all()

    def fd(d): return d.strftime('%Y-%m-%d') if d else None
    def sname(sid): s = Staff.query.get(sid); return s.name if s else ''
    return jsonify([{
        'id': r.id, 'store_id': r.store_id,
        'card_no': r.card_no or '', 'service_date': fd(r.service_date),
        'echo_media': r.echo_media or '',
        'staff_id': r.staff_id, 'staff_name': sname(r.staff_id),
        'customer_name': r.customer_name or '',
        'service_type': r.service_type or '',
        'visit_count': r.visit_count or 0,
        'status': r.status or '追客中',
        'memo': r.memo or '',
    } for r in records])


@app.route("/api/customer-service-records", methods=["POST"])
@login_required
def api_cs_records_add():
    data = request.get_json() or {}
    allowed = get_allowed_store_ids()
    if not allowed:
        return jsonify({'error': 'unauthorized'}), 403
    from datetime import datetime as _dt
    def pd(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    sid = int(data.get('store_id') or 0) or allowed[0]
    if sid not in allowed:
        sid = allowed[0]
    r = CustomerServiceRecord(
        store_id=sid,
        card_no=data.get('card_no', ''),
        service_date=pd(data.get('service_date')),
        echo_media=data.get('echo_media', ''),
        staff_id=int(data.get('staff_id') or 0) or None,
        customer_name=data.get('customer_name', ''),
        service_type=data.get('service_type', ''),
        visit_count=int(data.get('visit_count') or 0),
        status=data.get('status', '追客中'),
        memo=data.get('memo', ''),
    )
    db.session.add(r)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': r.id})


@app.route("/api/customer-service-records/<int:rid>", methods=["PUT"])
@login_required
def api_cs_records_update(rid):
    r = CustomerServiceRecord.query.get_or_404(rid)
    data = request.get_json() or {}
    from datetime import datetime as _dt
    def pd(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    r.card_no      = data.get('card_no', r.card_no)
    r.service_date = pd(data.get('service_date')) or r.service_date
    r.echo_media   = data.get('echo_media', r.echo_media)
    r.staff_id     = int(data.get('staff_id') or 0) or None
    r.customer_name = data.get('customer_name', r.customer_name)
    r.service_type = data.get('service_type', r.service_type)
    r.visit_count  = int(data.get('visit_count') or 0)
    r.status       = data.get('status', r.status)
    r.memo         = data.get('memo', r.memo)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/customer-service-records/<int:rid>", methods=["DELETE"])
@login_required
def api_cs_records_delete(rid):
    r = CustomerServiceRecord.query.get_or_404(rid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/leads")
@login_required
@manager_or_above_required
def leads_management():
    """反響管理ページ：リード一覧・追加"""
    stores = get_allowed_stores()
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    leads = (Lead.query
             .filter(Lead.store_id.in_(allowed_ids))
             .order_by(Lead.received_at.desc())
             .limit(100)
             .all())
    year, month = current_ym()
    store_id = allowed_ids[0] if allowed_ids else None
    return render_template("leads_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, store_id=store_id, now=datetime.now())


@app.route("/leads/add", methods=["POST"])
def leads_add():
    """反響追加（フォームPOST）"""
    data = request.form
    lead = Lead(
        source=data.get('source', ''),
        received_at=datetime.now(),
        status=data.get('status', '未対応'),
        assigned_staff_id=data.get('assigned_staff_id') or None,
        store_id=data.get('store_id') or None,
        customer_name=data.get('customer_name', ''),
        note=data.get('note', ''),
        line_added=data.get('line_added') == 'on',
    )
    db.session.add(lead)
    db.session.commit()
    return redirect(url_for('leads_management'))


@app.route("/api/leads/add", methods=["POST"])
def api_leads_add():
    """反響追加API（JSON/フォーム両対応）"""
    data = request.get_json() or request.form
    sid = safe_store_id(data.get('store_id'))
    if not sid:
        return jsonify({'error': 'unauthorized'}), 403
    lead = Lead(
        source=data.get('source') or data.get('media', ''),
        received_at=datetime.now(),
        status=data.get('status', '未対応'),
        assigned_staff_id=data.get('assigned_staff_id') or data.get('assignee_id') or None,
        store_id=sid,
        customer_name=data.get('customer_name', ''),
        note=data.get('note') or data.get('memo', ''),
        line_added=str(data.get('line_added', '0')) in ('1', 'true', 'True', 'on'),
    )
    db.session.add(lead)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': lead.id})


@app.route("/leads/status/<int:lead_id>")
def leads_status_update(lead_id):
    """ステータス更新（GETパラメータで受け取り）"""
    lead = Lead.query.get_or_404(lead_id)
    new_status = request.args.get('status', lead.status)
    lead.status = new_status
    db.session.commit()
    return redirect(url_for('leads_management'))


@app.route("/accounting")
@login_required
@manager_or_above_required
def accounting():
    """会計・PL管理ページ"""
    stores = get_allowed_stores()
    allowed_ids = [s.id for s in stores]
    year, month = current_ym()
    store_id = allowed_ids[0] if allowed_ids else None
    return render_template("accounting.html", stores=stores, year=year, month=month,
                           store_id=store_id, now=datetime.now())


@app.route("/staff-ranking")
@login_required
@block_super_admin
def staff_ranking():
    """スタッフランキングページ"""
    stores = get_allowed_stores()
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    year, month = current_ym()
    return render_template("staff_ranking.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now())


# ── 幹部向け管理ツール：データAPI（JSON） ────────────────

def _pl_profit_for(pls, y, m):
    """PLレコード群から利益合計を計算（PLCustomValue優先）"""
    total = 0
    for p in pls:
        cv = PLCustomValue.query.filter_by(store_id=p.store_id, year=y, month=m).all()
        ad_cvs    = [c for c in cv if c.item_type == '広告費']
        fixed_cvs = [c for c in cv if c.item_type == '固定費']
        var_cvs   = [c for c in cv if c.item_type == '変動費']
        if ad_cvs:
            ad_t = sum(c.amount for c in ad_cvs)
        else:
            ad_t = (p.ad_cost or 0) or sum(getattr(p, col, 0) or 0 for col in [
                'suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
                'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost'])
        lb_t = (p.labor_cost or 0) or ((p.regular_salary or 0) + (p.parttime_salary or 0) + (p.commission_pay or 0))
        ft = sum(c.amount for c in fixed_cvs)
        vt = sum(c.amount for c in var_cvs)
        total += (p.revenue or 0) - ad_t - lb_t - ft - vt
    return total


def _pl_ad_for(pls, y, m):
    """PLレコード群から広告費合計を計算（PLCustomValue優先）"""
    total = 0
    for p in pls:
        cv = PLCustomValue.query.filter_by(store_id=p.store_id, year=y, month=m).all()
        ad_cvs = [c for c in cv if c.item_type == '広告費']
        if ad_cvs:
            total += sum(c.amount for c in ad_cvs)
        else:
            total += (p.ad_cost or 0) or sum(getattr(p, col, 0) or 0 for col in [
                'suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
                'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost'])
    return total


@app.route("/api/kpi/by-store")
@login_required
def api_kpi_by_store():
    """店舗別KPIサマリー（売上/契約数/申込数/反響数/利益/経費）"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    stores = get_allowed_stores()
    result = []
    for store in stores:
        kpis = SalesKPI.query.filter_by(store_id=store.id, year=year, month=month).all()
        sales       = sum(k.sales_amount or 0 for k in kpis)
        contracts   = sum(k.contracts    or 0 for k in kpis)
        applications= sum(k.applications or 0 for k in kpis)
        inquiries   = sum(k.inquiries    or 0 for k in kpis)
        # 利益・経費はPLRecordから
        pl = PLRecord.query.filter_by(store_id=store.id, year=year, month=month).first()
        profit   = pl.net_profit if pl else None
        # 経費 = sales - profit (KPIベース)
        expenses = max(0, sales - (profit or 0)) if profit is not None else None
        # 入金済み申込件数
        from sqlalchemy import extract as _ex
        approved_cnt = ApplicationRecord.query.filter(
            ApplicationRecord.store_id == store.id,
            ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
            _ex('year',  ApplicationRecord.application_date) == year,
            _ex('month', ApplicationRecord.application_date) == month,
            db.or_(ApplicationRecord.ad_amount != 0, ApplicationRecord.brokerage_fee != 0),
            db.or_(ApplicationRecord.ad_amount <= 0, ApplicationRecord.ad_approved == True),
            db.or_(ApplicationRecord.brokerage_fee <= 0, ApplicationRecord.brokerage_approved == True),
        ).count()
        result.append({
            'store_id':    store.id,
            'store_name':  store.name,
            'sales':       sales,
            'contracts':   contracts,
            'applications': applications,
            'inquiries':   inquiries,
            'approved_apps': approved_cnt,
            'profit':      profit,
            'expenses':    expenses,
        })
    return jsonify(result)


@app.route("/api/kpi/summary")
def api_kpi_summary():
    """KPIサマリを返す（year/monthパラメータで任意月指定可）"""
    cy, cm = current_ym()
    year  = request.args.get('year',  type=int) or cy
    month = request.args.get('month', type=int) or cm

    allowed_ids = get_allowed_store_ids()

    def get_kpis(y, m):
        q = SalesKPI.query.filter_by(year=y, month=m)
        if allowed_ids:
            q = q.filter(SalesKPI.store_id.in_(allowed_ids))
        return q.all()

    def get_pl(y, m):
        q = PLRecord.query.filter_by(year=y, month=m)
        if allowed_ids:
            q = q.filter(PLRecord.store_id.in_(allowed_ids))
        return q.all()

    # 今月・前月
    kpis_now  = get_kpis(year, month)
    prev_m    = month - 1 if month > 1 else 12
    prev_y    = year if month > 1 else year - 1
    kpis_prev = get_kpis(prev_y, prev_m)
    pls_now   = get_pl(year, month)
    pls_prev  = get_pl(prev_y, prev_m)

    kpi_sales_now  = sum(k.sales_amount for k in kpis_now)
    kpi_sales_prev = sum(k.sales_amount for k in kpis_prev)
    contracts_now  = sum(k.contracts for k in kpis_now)
    contracts_prev = sum(k.contracts for k in kpis_prev)

    # 今月PL（経理データ優先）
    rev_now     = sum(p.revenue for p in pls_now)
    profit_now  = _pl_profit_for(pls_now, year, month)
    ad_now      = _pl_ad_for(pls_now, year, month)

    rev_prev    = sum(p.revenue for p in pls_prev)
    profit_prev = _pl_profit_for(pls_prev, prev_y, prev_m)
    ad_prev     = _pl_ad_for(pls_prev, prev_y, prev_m)

    # 経理データ優先で売上を決定（PLRecordなければKPIから取得）
    sales_now  = rev_now  if rev_now  > 0 else kpi_sales_now
    sales_prev = rev_prev if rev_prev > 0 else kpi_sales_prev

    # 広告ROI（今月）
    roi_now  = round(rev_now  / ad_now  * 100, 1) if ad_now  > 0 else 0
    roi_prev = round(rev_prev / ad_prev * 100, 1) if ad_prev > 0 else 0

    # 未対応反響（テナント分離：自分の店舗のみカウント）
    unhandled = Lead.query.filter(
        Lead.store_id.in_(allowed_ids),
        Lead.status == '未対応'
    ).count()

    # 着地予測（今月日割り）
    today_day = date.today().day
    days_in_month = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 28
    forecast = round(sales_now / today_day * days_in_month) if today_day > 0 else sales_now

    # 前年同月
    kpis_yoy  = get_kpis(year - 1, month)
    pls_yoy   = get_pl(year - 1, month)
    kpi_sales_yoy = sum(k.sales_amount for k in kpis_yoy)
    contracts_yoy = sum(k.contracts for k in kpis_yoy)
    rev_yoy    = sum(p.revenue for p in pls_yoy)
    sales_yoy  = rev_yoy if rev_yoy > 0 else kpi_sales_yoy
    profit_yoy = _pl_profit_for(pls_yoy, year - 1, month)
    ad_yoy     = _pl_ad_for(pls_yoy, year - 1, month)
    roi_yoy    = round(rev_yoy / ad_yoy * 100, 1) if ad_yoy > 0 else 0

    def diff_pct(now, prev):
        if prev == 0:
            return 0
        return round((now - prev) / prev * 100, 1)

    # 反響管理データがあればそちらを優先（自動連携）
    lead_stats_now  = LeadMediaStat.query.filter(LeadMediaStat.store_id.in_(allowed_ids), LeadMediaStat.year==year,  LeadMediaStat.month==month).all()
    lead_stats_prev = LeadMediaStat.query.filter(LeadMediaStat.store_id.in_(allowed_ids), LeadMediaStat.year==prev_y, LeadMediaStat.month==prev_m).all()
    if lead_stats_now:
        total_inquiries   = sum(s.inquiries    for s in lead_stats_now)
        total_visits      = sum(s.visits       for s in lead_stats_now)
        total_applications= sum(s.applications for s in lead_stats_now)
        total_contracts_l = sum(s.contracts    for s in lead_stats_now)
        total_cancels     = sum(s.cancellations for s in lead_stats_now)
    else:
        total_inquiries   = sum(k.inquiries    for k in kpis_now)
        total_visits      = sum(k.store_visits for k in kpis_now)
        total_applications= sum(k.applications for k in kpis_now)
        total_contracts_l = contracts_now
        total_cancels     = sum(k.cancellations for k in kpis_now)

    return jsonify({
        'year': year, 'month': month,
        # 今月売上
        'month_sales':         sales_now,
        'prev_month_sales':    sales_prev,
        'prev_year_sales':     sales_yoy,
        'mom_sales':           diff_pct(sales_now, sales_prev),
        'yoy_sales':           diff_pct(sales_now, sales_yoy),
        # 今月利益
        'month_profit':        profit_now,
        'prev_month_profit':   profit_prev,
        'prev_year_profit':    profit_yoy,
        'mom_profit':          diff_pct(profit_now, profit_prev),
        'yoy_profit':          diff_pct(profit_now, profit_yoy),
        # 広告ROI
        'roi':                 roi_now,
        'prev_month_roi':      roi_prev,
        'prev_year_roi':       roi_yoy,
        'mom_roi':             diff_pct(roi_now, roi_prev),
        'yoy_roi':             diff_pct(roi_now, roi_yoy),
        # 経費合計（売上 - 利益）
        'total_expenses':      max(0, sales_now  - profit_now),
        'prev_month_expenses': max(0, sales_prev - profit_prev),
        # 契約数
        'month_contracts':     contracts_now,
        'prev_month_contracts':contracts_prev,
        'prev_year_contracts': contracts_yoy,
        'mom_contracts':       diff_pct(contracts_now, contracts_prev),
        'yoy_contracts':       diff_pct(contracts_now, contracts_yoy),
        # ファネル詳細（反響管理データ優先）
        'total_inquiries':     total_inquiries,
        'total_store_visits':  total_visits,
        'total_viewings':      sum(k.viewings for k in kpis_now),
        'total_applications':  total_applications,
        'total_contracts':     total_contracts_l,
        'total_cancellations': total_cancels,
        'total_option_sales':  sum(k.option_sales for k in kpis_now),
        'from_lead_stats':     bool(lead_stats_now),
        'staff_count':         len(set(k.staff_id for k in kpis_now)),
        'visit_rate':          round(total_visits / total_inquiries * 100, 1) if total_inquiries else 0,
        'contract_rate':       round(contracts_now / total_inquiries * 100, 1) if total_inquiries else 0,
    })


@app.route("/api/kpi/monthly")
def api_kpi_monthly():
    """月次KPIデータをグラフ用に返す（from/toパラメータで期間指定可）"""
    store_id   = request.args.get('store_id', type=int)
    staff_id   = request.args.get('staff_id', type=int)   # スタッフ別フィルタ
    from_param = request.args.get('from')   # YYYY-MM
    to_param   = request.args.get('to')     # YYYY-MM
    months_data = []

    today = date.today()

    # 期間リストを生成
    def ym_range(fy, fm, ty, tm):
        base = fy * 12 + fm - 1
        end  = ty  * 12 + tm  - 1
        result = []
        for t in range(base, end + 1):
            result.append((t // 12, t % 12 + 1))
        return result

    if from_param and to_param:
        try:
            fy, fm = int(from_param[:4]), int(from_param[5:7])
            ty, tm = int(to_param[:4]),   int(to_param[5:7])
            periods = ym_range(fy, fm, ty, tm)
        except Exception:
            periods = None
    else:
        periods = None

    if not periods:
        # デフォルト：直近12ヶ月（当月含む）
        base_total = today.year * 12 + today.month - 1
        periods = [(t // 12, t % 12 + 1) for t in range(base_total - 11, base_total + 1)]

    allowed_ids = get_allowed_store_ids()
    # store_idパラメータ指定時はさらに絞り込み
    if store_id and store_id in allowed_ids:
        filter_ids = [store_id]
    elif store_id:
        filter_ids = []
    else:
        filter_ids = allowed_ids

    for y, m in periods:

        query = SalesKPI.query.filter_by(year=y, month=m)
        if filter_ids:
            query = query.filter(SalesKPI.store_id.in_(filter_ids))
        if staff_id:
            query = query.filter(SalesKPI.staff_id == staff_id)
        kpis = query.all()

        # スタッフ別フィルタ時はSalesKPIのみ使用（PLRecordは店舗単位のため）
        if staff_id:
            kpi_sales = sum(k.sales_amount for k in kpis)
            months_data.append({
                'label':       f'{y}/{m:02d}',
                'year':        y,
                'month':       m,
                'inquiries':   sum(k.inquiries for k in kpis),
                'contracts':   sum(k.contracts for k in kpis),
                'sales':       kpi_sales,
                'gross_profit': 0,
                'ad_cost':     0,
            })
            continue

        pls = PLRecord.query.filter_by(year=y, month=m)
        if filter_ids:
            pls = pls.filter(PLRecord.store_id.in_(filter_ids))
        pls = pls.all()

        def _calc_gp(pl):
            cv = PLCustomValue.query.filter_by(store_id=pl.store_id, year=y, month=m).all()
            ad_cvs = [c for c in cv if c.item_type == '広告費']
            fixed_cvs = [c for c in cv if c.item_type == '固定費']
            var_cvs   = [c for c in cv if c.item_type == '変動費']
            if ad_cvs:
                ad_t = sum(c.amount for c in ad_cvs)
            else:
                ad_t = (pl.ad_cost or 0) or sum(getattr(pl, col, 0) or 0 for col in [
                    'suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
                    'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost'])
            lb_t = (pl.labor_cost or 0) or ((pl.regular_salary or 0)+(pl.parttime_salary or 0)+(pl.commission_pay or 0))
            ft = sum(c.amount for c in fixed_cvs)
            vt = sum(c.amount for c in var_cvs)
            return (pl.revenue or 0) - ad_t - lb_t - ft - vt

        pl_revenue = sum(p.revenue or 0 for p in pls)
        kpi_sales  = sum(k.sales_amount for k in kpis)
        months_data.append({
            'label':       f'{y}/{m:02d}',
            'year':        y,
            'month':       m,
            'inquiries':   sum(k.inquiries for k in kpis),
            'contracts':   sum(k.contracts for k in kpis),
            'sales':       pl_revenue if pl_revenue > 0 else kpi_sales,
            'gross_profit':sum(_calc_gp(p) for p in pls),
            'ad_cost':     sum(p.ad_cost or 0 for p in pls),
        })

    return jsonify(months_data)


@app.route("/api/kpi/staff")
def api_kpi_staff():
    """スタッフ別KPIデータを返す"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    store_id    = request.args.get('store_id', type=int)
    allowed_ids = get_allowed_store_ids()

    # テナント分離: 許可されたstore_idのみ
    query = SalesKPI.query.filter_by(year=year, month=month).filter(SalesKPI.store_id.in_(allowed_ids))
    if store_id and store_id in allowed_ids:
        query = query.filter(SalesKPI.store_id == store_id)
    kpis = query.all()

    # 顧客管理表(ApplicationRecord)からスタッフ別の申込数・付帯契約数を集計（成約率算出用）
    app_q = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
        db.extract('year',  ApplicationRecord.application_date) == year,
        db.extract('month', ApplicationRecord.application_date) == month,
    )
    if store_id and store_id in allowed_ids:
        app_q = app_q.filter(ApplicationRecord.store_id == store_id)
    app_stats = {}
    for a in app_q.all():
        st = app_stats.setdefault(a.staff_id, {'app': 0, 'll': 0, 'fire': 0, 'mv': 0})
        st['app'] += 1
        if a.lifeline:       st['ll']   += 1
        if a.fire_insurance: st['fire'] += 1
        if a.moving:         st['mv']   += 1

    result = []
    for kpi in kpis:
        staff = Staff.query.get(kpi.staff_id)
        store = Store.query.get(kpi.store_id)
        result.append({
            'kpi_id':      kpi.id,
            'staff_id':    kpi.staff_id,
            'staff_name':  staff.name if staff else '不明',
            'store_name':  store.name if store else '不明',
            'role':        staff.role if staff else '',
            'inquiries':   kpi.inquiries,
            'store_visits':kpi.store_visits,
            'viewings':    kpi.viewings,
            'applications':kpi.applications,
            'contracts':   kpi.contracts,
            'cancellations': kpi.cancellations,
            'sales_amount':kpi.sales_amount,
            'option_sales':kpi.option_sales,
            'estimated_sales':     kpi.estimated_sales or 0,
            'target_sales':        kpi.target_sales or 0,
            'fire_insurance_count':kpi.fire_insurance_count or 0,
            'lifeline_count':      kpi.lifeline_count or 0,
            'moving_count':        kpi.moving_count or 0,
        })
        # 顧客管理表ベースの付帯契約数・成約率（申込数に対して）
        st = app_stats.get(kpi.staff_id, {'app': 0, 'll': 0, 'fire': 0, 'mv': 0})
        app_ct = st['app']
        _r = lambda c: round(c / app_ct * 100, 1) if app_ct else 0
        result[-1].update({
            'app_count_real':  app_ct,
            'll_contracts':    st['ll'],
            'fire_contracts':  st['fire'],
            'moving_contracts': st['mv'],
            'll_rate':         _r(st['ll']),
            'fire_rate':       _r(st['fire']),
            'moving_rate':     _r(st['mv']),
        })

    # 売上降順でソート
    result.sort(key=lambda x: x['sales_amount'], reverse=True)
    return jsonify(result)


@app.route("/api/leads/summary")
def api_leads_summary():
    """反響サマリを返す（媒体別・ステータス別）"""
    store_id  = request.args.get('store_id', type=int)
    year      = request.args.get('year',  type=int)
    month     = request.args.get('month', type=int)
    allowed_ids = get_allowed_store_ids()

    # テナント分離: 許可されたstore_idのみ
    query = Lead.query.filter(Lead.store_id.in_(allowed_ids))
    if store_id and store_id in allowed_ids:
        query = query.filter(Lead.store_id == store_id)
    if year and month:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        query = query.filter(Lead.received_at >= start, Lead.received_at < end)

    leads = query.all()

    # 媒体別集計
    by_source = {}
    for lead in leads:
        src = lead.source or '不明'
        by_source.setdefault(src, 0)
        by_source[src] += 1

    # ステータス別集計
    by_status = {}
    for lead in leads:
        st = lead.status or '不明'
        by_status.setdefault(st, 0)
        by_status[st] += 1

    # リード一覧
    leads_list = []
    for lead in leads:
        staff = Staff.query.get(lead.assigned_staff_id) if lead.assigned_staff_id else None
        leads_list.append({
            'id':            lead.id,
            'received_at':   lead.received_at.strftime('%Y-%m-%d %H:%M') if lead.received_at else '',
            'media':         lead.source or '',
            'source':        lead.source or '',
            'customer_name': lead.customer_name or '',
            'status':        lead.status or '未対応',
            'assignee':      staff.name if staff else '',
            'assigned_staff_id': lead.assigned_staff_id,
            'memo':          lead.note or '',
            'note':          lead.note or '',
            'line_added':    lead.line_added,
            'store_id':      lead.store_id,
        })

    # 媒体別サマリー（media_summary形式）
    media_summary_dict = {}
    for lead in leads:
        src = lead.source or '不明'
        if src not in media_summary_dict:
            media_summary_dict[src] = {'media': src, 'count': 0, 'cost': 0, 'visits': 0, 'contracts': 0}
        media_summary_dict[src]['count'] += 1
        if lead.status in ('来店', '申込', '契約', '内見'):
            media_summary_dict[src]['visits'] += 1
        if lead.status == '契約':
            media_summary_dict[src]['contracts'] += 1
    media_summary = list(media_summary_dict.values())

    # 月次推移（過去6ヶ月）
    monthly_labels = []
    monthly_datasets = {}
    today = date.today()
    for i in range(5, -1, -1):
        t = today.replace(day=1)
        for _ in range(i):
            t = (t - timedelta(days=1)).replace(day=1)
        y, m = t.year, t.month
        lbl = f'{y}/{m:02d}'
        monthly_labels.append(lbl)
        period_leads = Lead.query.filter(
            Lead.received_at >= datetime(y, m, 1),
            Lead.received_at < (datetime(y+1, 1, 1) if m == 12 else datetime(y, m+1, 1))
        ).all()
        for lead in period_leads:
            src = lead.source or '不明'
            if src not in monthly_datasets:
                monthly_datasets[src] = [0] * 6
            idx = 5 - i
            monthly_datasets[src][idx] += 1

    return jsonify({
        'total':            len(leads),
        'by_source':        by_source,
        'by_status':        by_status,
        'line_added_count': sum(1 for l in leads if l.line_added),
        'leads':            leads_list,
        'media_summary':    media_summary,
        'monthly':          {'labels': monthly_labels, 'datasets': monthly_datasets},
    })


_PL_AD_COL = {
    'SUUMO': 'suumo_cost', "HOME'S": 'homes_cost', 'アットホーム': 'athome_cost',
    'Instagram': 'instagram_cost', 'TikTok': 'tiktok_cost', '自社HP': 'hp_cost',
    'LINE': 'line_cost', 'MEO': 'meo_cost', 'Google広告': 'google_ads_cost',
}


def _auto_lead_media_stats(store_id, year, month):
    """媒体別の月次統計を各管理表から自動集計する（⑰）。
    反響数/返信/LINE追加→反響管理表, 接客数→接客管理表,
    申込/契約/キャンセル/売上見込み→顧客管理表, 広告費→経理PL"""
    from types import SimpleNamespace
    import calendar as _cal
    last_day = _cal.monthrange(year, month)[1]
    m_start = date(year, month, 1)
    m_end   = date(year, month, last_day)

    agg = {}  # media -> dict
    def slot(media):
        key = media or '不明'
        if key not in agg:
            agg[key] = dict(media=key, inquiries=0, replies=0, line_added=0, visits=0,
                            applications=0, contracts=0, cancellations=0, cancel_amount=0,
                            estimated_sales=0, ad_cost=0)
        return agg[key]

    # 反響管理表（EchoRecord）→ 反響数・返信・LINE追加
    echoes = EchoRecord.query.filter(
        EchoRecord.store_id == store_id,
        EchoRecord.echo_date >= m_start, EchoRecord.echo_date <= m_end,
    ).all()
    for e in echoes:
        s = slot(e.media)
        s['inquiries'] += 1
        if e.has_reply: s['replies'] += 1
        if e.has_line:  s['line_added'] += 1

    # 接客管理表（CustomerServiceRecord）→ 接客数
    css = CustomerServiceRecord.query.filter(
        CustomerServiceRecord.store_id == store_id,
        CustomerServiceRecord.service_date >= m_start,
        CustomerServiceRecord.service_date <= m_end,
    ).all()
    for c in css:
        slot(c.echo_media)['visits'] += 1

    # 顧客管理表（ApplicationRecord）→ 申込・契約・キャンセル・売上見込み
    apps = ApplicationRecord.query.filter(
        ApplicationRecord.store_id == store_id,
        db.extract('year',  ApplicationRecord.application_date) == year,
        db.extract('month', ApplicationRecord.application_date) == month,
    ).all()
    for a in apps:
        s = slot(a.media)
        if a.status in ('キャンセル', 'キャンセル振替'):
            s['cancellations'] += 1
            s['cancel_amount'] += _record_total_amount(a)
        else:
            s['applications'] += 1
            s['estimated_sales'] += _record_total_amount(a)
            if a.status == '契約':
                s['contracts'] += 1

    # 経理PL → 広告費（媒体別）
    pl = PLRecord.query.filter_by(store_id=store_id, year=year, month=month).first()
    if pl:
        for media, col in _PL_AD_COL.items():
            v = getattr(pl, col, 0) or 0
            if v:
                slot(media)['ad_cost'] += v
    for cv in PLCustomValue.query.filter_by(store_id=store_id, year=year, month=month).all():
        if cv.item_type == '広告費' and (cv.amount or 0):
            slot(cv.item_name)['ad_cost'] += cv.amount or 0

    # idは持たない（自動集計のため編集不可）
    return [SimpleNamespace(id=None, **v) for v in agg.values()]


@app.route("/api/leads/monthly-stats")
@login_required
def api_leads_monthly_stats():
    """媒体別月次反響統計を返す（各管理表から自動集計）"""
    cy, cm = current_ym()
    year  = request.args.get('year',  type=int) or cy
    month = request.args.get('month', type=int) or cm
    # ignore_active=True で保存時と同じ基準で店舗を解決する
    allowed = get_allowed_store_ids()
    if not allowed:
        return jsonify({'stats': [], 'totals': {}, 'trend': []}), 200
    req_sid = request.args.get('store_id', type=int) or 0
    store_id = req_sid if req_sid and req_sid in allowed else allowed[0]

    stats = _auto_lead_media_stats(store_id, year, month)

    # 前月比較
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    prev_stats = _auto_lead_media_stats(store_id, prev_y, prev_m)
    prev_dict = {s.media: s for s in prev_stats}

    def safe_div(a, b, pct=False):
        if not b: return 0
        return round(a / b * (100 if pct else 1))

    def pct_diff(cur, prv):
        """前月比（%）を返す。Noneなら比較不可"""
        if prv is None or prv == 0: return None
        return round((cur - prv) / prv * 100, 1)

    result = []
    for s in stats:
        prev = prev_dict.get(s.media)
        # 売上見込みベースでROAS計算
        roas = round(s.estimated_sales / s.ad_cost * 100, 1) if s.ad_cost else 0
        result.append({
            'id': s.id,
            'media': s.media,
            'inquiries': s.inquiries,
            'replies': s.replies,
            'line_added': s.line_added,
            'visits': s.visits,
            'applications': s.applications,
            'contracts': s.contracts,
            'cancellations': s.cancellations,
            'cancel_amount': s.cancel_amount,
            'estimated_sales': s.estimated_sales,
            'ad_cost': s.ad_cost,
            'cvr': safe_div(s.contracts, s.inquiries, pct=True),
            'cpa': safe_div(s.ad_cost, s.inquiries) if s.inquiries else 0,
            'cpo': safe_div(s.ad_cost, s.contracts) if s.contracts else 0,
            'roas': roas,
            # 前月比
            'prev_inquiries':    prev.inquiries    if prev else None,
            'prev_applications': prev.applications if prev else None,
            'prev_contracts':    prev.contracts    if prev else None,
            'prev_estimated':    prev.estimated_sales if prev else None,
            'prev_ad_cost':      prev.ad_cost      if prev else None,
            'mom_inq':  pct_diff(s.inquiries,     prev.inquiries     if prev else None),
            'mom_app':  pct_diff(s.applications,  prev.applications  if prev else None),
            'mom_con':  pct_diff(s.contracts,     prev.contracts     if prev else None),
            'mom_est':  pct_diff(s.estimated_sales, prev.estimated_sales if prev else None),
        })

    # 合計
    def total(field):
        return sum(getattr(s, field, 0) or 0 for s in stats)

    def ptotal(field):
        return sum(getattr(s, field, 0) or 0 for s in prev_stats)

    tot_inq = total('inquiries')
    tot_app = total('applications')
    tot_con = total('contracts')
    tot_est = total('estimated_sales')
    tot_ad  = total('ad_cost')
    totals = {
        'media': '合計',
        'inquiries':      tot_inq,
        'replies':        total('replies'),
        'line_added':     total('line_added'),
        'visits':         total('visits'),
        'applications':   tot_app,
        'contracts':      tot_con,
        'cancellations':  total('cancellations'),
        'cancel_amount':  total('cancel_amount'),
        'estimated_sales': tot_est,
        'ad_cost':        tot_ad,
        'cvr':  safe_div(tot_con, tot_inq, pct=True),
        'cpa':  safe_div(tot_ad, tot_inq) if tot_inq else 0,
        'cpo':  safe_div(tot_ad, tot_con) if tot_con else 0,
        'roas': round(tot_est / tot_ad * 100, 1) if tot_ad else 0,
        # 前月合計
        'prev_inquiries':   ptotal('inquiries'),
        'prev_applications': ptotal('applications'),
        'prev_contracts':   ptotal('contracts'),
        'prev_estimated':   ptotal('estimated_sales'),
        'mom_inq': pct_diff(tot_inq, ptotal('inquiries')),
        'mom_app': pct_diff(tot_app, ptotal('applications')),
        'mom_con': pct_diff(tot_con, ptotal('contracts')),
        'mom_est': pct_diff(tot_est, ptotal('estimated_sales')),
    }

    # 月次トレンド（過去6ヶ月）
    trend = []
    for i in range(5, -1, -1):
        tm, ty = month - i, year
        while tm <= 0:
            tm += 12; ty -= 1
        ms = _auto_lead_media_stats(store_id, ty, tm)
        trend.append({
            'label': f'{ty}/{tm:02d}',
            'inquiries':      sum(s.inquiries for s in ms),
            'contracts':      sum(s.contracts for s in ms),
            'estimated_sales': sum(s.estimated_sales for s in ms),
        })

    return jsonify({'stats': result, 'totals': totals, 'trend': trend, 'year': year, 'month': month})


@app.route("/api/leads/trend")
def api_leads_trend():
    """反響月次トレンドを返す（from/toパラメータで期間指定可、デフォルト直近6ヶ月）"""
    store_id   = safe_store_id(request.args.get('store_id', type=int))
    from_param = request.args.get('from')
    to_param   = request.args.get('to')
    today = date.today()

    if from_param and to_param:
        try:
            fy, fm = int(from_param[:4]), int(from_param[5:7])
            ty, tm_e = int(to_param[:4]), int(to_param[5:7])
            base = fy * 12 + fm - 1
            end  = ty * 12 + tm_e - 1
            periods = [(t // 12, t % 12 + 1) for t in range(base, end + 1)]
        except Exception:
            periods = None
    else:
        periods = None

    if not periods:
        base_total = today.year * 12 + today.month - 1
        periods = [(t // 12, t % 12 + 1) for t in range(base_total - 5, base_total + 1)]

    trend = []
    for y, m in periods:
        ms = LeadMediaStat.query.filter_by(store_id=store_id, year=y, month=m).all()
        trend.append({
            'label':           f'{y}/{m:02d}',
            'inquiries':       sum(s.inquiries for s in ms),
            'contracts':       sum(s.contracts for s in ms),
            'estimated_sales': sum(s.estimated_sales for s in ms),
        })
    return jsonify(trend)


@app.route("/api/leads/monthly-stats", methods=["POST"])
@login_required
def api_leads_monthly_stats_input():
    """媒体別月次反響統計を手動入力"""
    data = request.get_json() or request.form
    year  = int(data.get('year', current_ym()[0]))
    month = int(data.get('month', current_ym()[1]))
    # ignore_active=True で保存・取得ともに同じ基準にする
    allowed = get_allowed_store_ids()
    if not allowed:
        return jsonify({'error': 'unauthorized'}), 403
    try:
        req_sid = int(data.get('store_id') or 0)
    except (TypeError, ValueError):
        req_sid = 0
    store_id = req_sid if req_sid and req_sid in allowed else allowed[0]
    media = data.get('media', '').strip()
    if not media:
        return jsonify({'error': '媒体名は必須です'}), 400

    stat = LeadMediaStat.query.filter_by(store_id=store_id, year=year, month=month, media=media).first()
    if not stat:
        stat = LeadMediaStat(store_id=store_id, year=year, month=month, media=media)
        db.session.add(stat)

    for field in ['inquiries','replies','line_added','visits','applications','contracts','cancellations']:
        v = data.get(field)
        if v is not None:
            setattr(stat, field, int(float(v) or 0))
    for field in ['cancel_amount','estimated_sales','actual_payment','ad_cost']:
        v = data.get(field)
        if v is not None:
            setattr(stat, field, float(v or 0))

    db.session.commit()
    return jsonify({'status': 'ok', 'id': stat.id, 'store_id': store_id, 'year': year, 'month': month})


@app.route("/api/leads/monthly-stats/<int:stat_id>", methods=["PUT"])
def api_leads_monthly_stats_update(stat_id):
    """媒体別月次反響統計を更新"""
    data = request.get_json() or request.form
    stat = LeadMediaStat.query.get_or_404(stat_id)

    if data.get('media'):
        stat.media = data.get('media').strip()
    for field in ['inquiries','replies','line_added','visits','applications','contracts','cancellations']:
        v = data.get(field)
        if v is not None:
            setattr(stat, field, int(float(v) or 0))
    for field in ['cancel_amount','estimated_sales','actual_payment','ad_cost']:
        v = data.get(field)
        if v is not None:
            setattr(stat, field, float(v or 0))

    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/leads/monthly-stats/<int:stat_id>", methods=["DELETE"])
def api_leads_monthly_stats_delete(stat_id):
    """媒体別月次反響統計を削除"""
    stat = LeadMediaStat.query.get_or_404(stat_id)
    db.session.delete(stat)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/leads/import-excel-stats", methods=["POST"])
def api_leads_import_excel_stats():
    """反響統計ExcelをインポートしてLeadMediaStatに保存"""
    year  = int(request.form.get('year',  current_ym()[0]))
    month = int(request.form.get('month', current_ym()[1]))
    store_id = safe_store_id(request.form.get('store_id', type=int))
    if not store_id:
        return jsonify({'error': 'unauthorized'}), 403

    if 'file' not in request.files:
        return jsonify({'error': 'ファイルが必要です'}), 400

    import openpyxl
    file = request.files['file']
    wb = openpyxl.load_workbook(file, data_only=True)
    ws = wb.active

    SKIP = {'総費用対効果', '総反響数', None, ''}

    def safe_int(v):
        if v is None or str(v) in ('#DIV/0!', 'nan', ''): return 0
        try: return int(float(str(v)))
        except: return 0

    def safe_float(v):
        if v is None or str(v) in ('#DIV/0!', 'nan', ''): return 0.0
        try: return float(str(v))
        except: return 0.0

    imported = 0
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or len(row) < 2: continue
        media = row[1]
        if not media or str(media).strip() in SKIP: continue
        media = str(media).strip()

        stat = LeadMediaStat.query.filter_by(store_id=store_id, year=year, month=month, media=media).first()
        if not stat:
            stat = LeadMediaStat(store_id=store_id, year=year, month=month, media=media)
            db.session.add(stat)

        stat.inquiries      = safe_int(row[2] if len(row) > 2 else None)
        stat.replies        = safe_int(row[3] if len(row) > 3 else None)
        stat.line_added     = safe_int(row[4] if len(row) > 4 else None)
        stat.visits         = safe_int(row[5] if len(row) > 5 else None)
        stat.applications   = safe_int(row[6] if len(row) > 6 else None)
        stat.contracts      = safe_int(row[7] if len(row) > 7 else None)
        stat.cancellations  = safe_int(row[8] if len(row) > 8 else None)
        stat.cancel_amount  = safe_float(row[9] if len(row) > 9 else None)
        stat.estimated_sales= safe_float(row[10] if len(row) > 10 else None)
        stat.actual_payment = safe_float(row[11] if len(row) > 11 else None)
        stat.ad_cost        = safe_float(row[12] if len(row) > 12 else None)
        imported += 1

    db.session.commit()
    return jsonify({'status': 'ok', 'imported': imported, 'year': year, 'month': month})


def _get_ad_items(store_id, y, m, pl=None):
    """広告費カスタム行を取得。なければ固定列からフォールバック"""
    ad_cvs = PLCustomValue.query.filter_by(store_id=store_id, year=y, month=m, item_type='広告費').all()
    if ad_cvs:
        return [{'name': v.item_name, 'amount': v.amount} for v in ad_cvs]
    if not pl:
        return []
    _AD_COLS = [('suumo_cost','SUUMO'),('homes_cost',"HOME'S"),('athome_cost','at home'),
                ('instagram_cost','Instagram'),('tiktok_cost','TikTok'),('google_ads_cost','Google広告'),
                ('line_cost','LINE'),('hp_cost','HP'),('meo_cost','MEO'),('other_ad_cost','その他')]
    return [{'name': n, 'amount': getattr(pl, col, 0) or 0}
            for col, n in _AD_COLS if (getattr(pl, col, 0) or 0) > 0]


@app.route("/api/pl/summary")
def api_pl_summary():
    """PLサマリを返す（店舗別・月次）"""
    store_id    = request.args.get('store_id', type=int)
    year        = request.args.get('year',  type=int) or current_ym()[0]
    month       = request.args.get('month', type=int) or current_ym()[1]
    allowed_ids = get_allowed_store_ids()

    # テナント分離: 許可されたstore_idのみ
    query = PLRecord.query.filter_by(year=year, month=month).filter(PLRecord.store_id.in_(allowed_ids))
    if store_id and store_id in allowed_ids:
        query = query.filter(PLRecord.store_id == store_id)
    pls = query.all()

    result = []
    for pl in pls:
        store = Store.query.get(pl.store_id)
        # カスタム費用（固定費・変動費・広告費）を取得
        custom_vals = PLCustomValue.query.filter_by(
            store_id=pl.store_id, year=year, month=month
        ).all()
        fixed_items    = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '固定費']
        variable_items = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '変動費']
        custom_items   = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') not in ('固定費','変動費','広告費')]
        ad_items       = _get_ad_items(pl.store_id, year, month, pl)

        # 広告費合計
        ad_cost_val = sum(i['amount'] for i in ad_items)

        # 人件費合計
        labor_total = (pl.regular_salary or 0) + (pl.parttime_salary or 0) + (pl.commission_pay or 0)
        labor_cost_val = pl.labor_cost if (pl.labor_cost and pl.labor_cost > 0) else labor_total

        # カスタム固定費・変動費合計
        fixed_total    = sum(item['amount'] for item in fixed_items)
        variable_total = sum(item['amount'] for item in variable_items)

        # 粗利・営業利益 = 売上 - 全経費
        total_cost = ad_cost_val + labor_cost_val + fixed_total + variable_total
        gross_profit_calc = pl.revenue - total_cost
        # DBに保存された gross_profit があればそちらを使用（0の場合は計算値）
        gross_profit_val = pl.gross_profit if pl.gross_profit != 0 else gross_profit_calc
        operating_profit = gross_profit_calc  # 営業利益 = 粗利（全経費控除後）

        result.append({
            'pl_id':          pl.id,
            'store_id':       pl.store_id,
            'store_name':     store.name if store else '不明',
            'revenue':        pl.revenue,
            'gross_profit':   gross_profit_calc,   # 自動計算値
            'gross_margin':   round(gross_profit_calc / pl.revenue * 100, 1) if pl.revenue else 0,
            'ad_cost':        ad_cost_val,
            'labor_cost':     labor_cost_val,
            'other_fixed':    fixed_total,
            'other_variable': variable_total,
            'operating_profit': operating_profit,
            'op_margin':      round(operating_profit / pl.revenue * 100, 1) if pl.revenue else 0,
            # 収入詳細
            'brokerage_fee':       pl.brokerage_fee or 0,
            'ad_income':           pl.ad_income or 0,
            'lifeline_income':     pl.lifeline_income or 0,
            'moving_income':       pl.moving_income or 0,
            'fire_insurance_income': pl.fire_insurance_income or 0,
            'other_income':        pl.other_income or 0,
            # 広告費詳細（動的行）
            'ad_items':       ad_items,
            # 人件費詳細
            'regular_salary':  pl.regular_salary or 0,
            'parttime_salary': pl.parttime_salary or 0,
            'social_insurance': pl.commission_pay or 0,  # commission_payを社会保険料として使用
            # 固定費詳細
            'pl_rent':       pl.pl_rent or 0,
            'pl_parking':    pl.pl_parking or 0,
            'pl_copier':     pl.pl_copier or 0,
            'pl_internet':   pl.pl_internet or 0,
            'pl_consultant': pl.pl_consultant or 0,
            'pl_insurance':  pl.pl_insurance or 0,
            'pl_cloud':      pl.pl_cloud or 0,
            # カスタム費用
            'fixed_items':    fixed_items,
            'variable_items': variable_items,
            'custom_items':   custom_items,
        })

    # テンプレート一覧（type別・テナント分離）
    _pl_sid = safe_store_id()
    all_items = PLCustomItem.query.filter_by(store_id=_pl_sid).order_by(PLCustomItem.sort_order).all() if _pl_sid else []
    template_fixed    = [i.name for i in all_items if (i.item_type or '固定費') == '固定費']
    template_variable = [i.name for i in all_items if (i.item_type or '固定費') == '変動費']
    template_items    = [i.name for i in all_items if (i.item_type or '固定費') not in ('固定費','変動費')]

    # prev_month / prev_year サポート
    def get_prev_summary(y, m):
        # 店舗フィルタ（選択中の店舗のみ）
        pq = PLRecord.query.filter_by(year=y, month=m)
        if allowed_ids:
            pq = pq.filter(PLRecord.store_id.in_(allowed_ids))
        if store_id and store_id in allowed_ids:
            pq = pq.filter(PLRecord.store_id == store_id)
        prev_pls = pq.all()
        if not prev_pls:
            return None
        pl = prev_pls[0]
        cv = PLCustomValue.query.filter_by(store_id=pl.store_id, year=y, month=m).all()
        fi = [{'name': v.item_name, 'amount': v.amount} for v in cv if (v.item_type or '固定費') == '固定費']
        vi = [{'name': v.item_name, 'amount': v.amount} for v in cv if (v.item_type or '固定費') == '変動費']
        ai = _get_ad_items(pl.store_id, y, m, pl)
        ad_v = sum(i['amount'] for i in ai) if ai else (pl.ad_cost or 0)
        lb_t = (pl.regular_salary or 0)+(pl.parttime_salary or 0)+(pl.commission_pay or 0)
        lb_v = pl.labor_cost if (pl.labor_cost and pl.labor_cost > 0) else lb_t
        ft = sum(i['amount'] for i in fi)
        vt = sum(i['amount'] for i in vi)
        gp = (pl.revenue or 0) - ad_v - lb_v - ft - vt
        return {
            'revenue': pl.revenue, 'gross_profit': gp, 'ad_cost': ad_v,
            'labor_cost': lb_v, 'other_fixed': ft, 'other_variable': vt,
            'operating_profit': gp,
            'ad_items': ai,
            'regular_salary': pl.regular_salary or 0, 'parttime_salary': pl.parttime_salary or 0,
            'social_insurance': pl.commission_pay or 0,
            'fixed_items': fi, 'variable_items': vi,
        }

    pm = month - 1 if month > 1 else 12
    py_m = year if month > 1 else year - 1
    prev_month = get_prev_summary(py_m, pm)
    prev_year  = get_prev_summary(year - 1, month)

    return jsonify({
        'year': year, 'month': month,
        'stores': result,
        'current': result[0] if result else None,
        'prev_month': prev_month,
        'prev_year':  prev_year,
        'template_fixed': template_fixed,
        'template_variable': template_variable,
        'template_items': template_items,
    })


@app.route("/api/pl/custom-items")
def api_pl_custom_items():
    """PLカスタム項目テンプレート一覧（type別・テナント分離）"""
    sid = safe_store_id()
    items = PLCustomItem.query.filter_by(store_id=sid).order_by(PLCustomItem.sort_order).all() if sid else []
    return jsonify({
        'fixed':    [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') == '固定費'],
        'variable': [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') == '変動費'],
        'other':    [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') not in ('固定費','変動費')],
    })


@app.route("/api/pl/prev-month-data")
def api_pl_prev_month_data():
    """前月の固定費・PLデータを返す（新規入力時の自動入力用）"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    allowed_ids = get_allowed_store_ids()
    store_id = request.args.get('store_id', type=int)
    # テナント分離: store_idが未指定または許可外なら先頭の許可店舗を使用
    if not store_id or store_id not in allowed_ids:
        store_id = allowed_ids[0] if allowed_ids else None
    if not store_id:
        return jsonify({})

    # 前月
    pm = month - 1 if month > 1 else 12
    py = year if month > 1 else year - 1

    pl = PLRecord.query.filter_by(store_id=store_id, year=py, month=pm).first()
    custom_vals = PLCustomValue.query.filter_by(store_id=store_id, year=py, month=pm).all()

    fixed_items    = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '固定費']
    variable_items = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '変動費']
    ad_items       = _get_ad_items(store_id, py, pm, pl)

    result = {
        'fixed_items': fixed_items,
        'variable_items': variable_items,
        'ad_items': ad_items,
    }
    if pl:
        result.update({
            'regular_salary':  pl.regular_salary or 0,
            'parttime_salary': pl.parttime_salary or 0,
            'social_insurance': pl.commission_pay or 0,
        })
    return jsonify(result)


@app.route("/api/staff/ranking")
def api_staff_ranking():
    """スタッフランキングデータを返す"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    metric = request.args.get('metric', 'sales_amount')  # ランキング指標
    store_id = request.args.get('store_id', type=int)

    # 月間 or 累計（年間）の切り替え
    period = request.args.get('period', 'month')  # 'month' or 'year'

    if period == 'year':
        # 年間累計
        query = SalesKPI.query.filter_by(year=year)
    else:
        query = SalesKPI.query.filter_by(year=year, month=month)

    # 店舗フィルタ（テナント分離）
    _allowed = get_allowed_store_ids()
    if store_id and store_id in _allowed:
        query = query.filter_by(store_id=store_id)
    elif _allowed:
        query = query.filter(SalesKPI.store_id.in_(_allowed))
    kpis = query.all()

    # スタッフ別に集計
    staff_totals = {}
    for kpi in kpis:
        sid = kpi.staff_id
        if sid not in staff_totals:
            staff = Staff.query.get(sid)
            store = Store.query.get(kpi.store_id)
            staff_totals[sid] = {
                'staff_id':    sid,
                'staff_name':  staff.name if staff else '不明',
                'role':        staff.role if staff else '',
                'store_name':  store.name if store else '不明',
                'inquiries':   0,
                'store_visits':0,
                'viewings':    0,
                'applications':0,
                'contracts':   0,
                'sales_amount':0,
                'option_sales':0,
            }
        for field in ['inquiries', 'store_visits', 'viewings', 'applications',
                      'contracts', 'sales_amount', 'option_sales']:
            staff_totals[sid][field] += getattr(kpi, field)

    # 指定指標でソート
    ranking = sorted(staff_totals.values(), key=lambda x: x.get(metric, 0), reverse=True)
    # 順位付け
    for rank, item in enumerate(ranking, 1):
        item['rank'] = rank

    return jsonify({'year': year, 'month': month, 'period': period, 'ranking': ranking})


# ── 幹部向け管理ツール：データ入力API ────────────────────

@app.route("/api/kpi/input", methods=["POST"])
@login_required
def api_kpi_input():
    """KPIデータを入力・更新する"""
    import traceback
    try:
        data = request.get_json() or request.form
        staff_id_raw = data.get('staff_id', 0)
        try:
            staff_id = int(staff_id_raw)
        except (TypeError, ValueError):
            staff_id = 0
        store_id = safe_store_id(data.get('store_id'))
        if not store_id:
            return jsonify({'error': 'store not found', 'allowed': get_allowed_store_ids()}), 403
        if not staff_id or staff_id <= 0:
            return jsonify({'error': 'staff_id は必須です', 'got': staff_id_raw}), 400
        staff = db.session.get(Staff, staff_id) if hasattr(db.session, 'get') else Staff.query.get(staff_id)
        if not staff:
            return jsonify({'error': f'スタッフ(id={staff_id})が見つかりません'}), 400
        year  = int(data.get('year',  current_ym()[0]))
        month = int(data.get('month', current_ym()[1]))

        kpi = SalesKPI.query.filter_by(staff_id=staff_id, store_id=store_id,
                                        year=year, month=month).first()
        if not kpi:
            kpi = SalesKPI(staff_id=staff_id, store_id=store_id, year=year, month=month,
                           inquiries=0, store_visits=0, viewings=0, applications=0,
                           contracts=0, cancellations=0, sales_amount=0.0,
                           option_sales=0.0, estimated_sales=0.0, target_sales=0.0,
                           fire_insurance_count=0, lifeline_count=0, moving_count=0)
            db.session.add(kpi)
            db.session.flush()  # idを確定させてからフィールドを設定

        kpi.inquiries    = int(data.get('inquiries',     0) or 0)
        kpi.store_visits = int(data.get('store_visits',  0) or 0)
        kpi.viewings     = int(data.get('viewings',      0) or 0)
        kpi.applications = int(data.get('applications',  0) or 0)
        kpi.contracts    = int(data.get('contracts',     0) or 0)
        kpi.cancellations= int(data.get('cancellations', 0) or 0)
        kpi.sales_amount = float(data.get('sales_amount',  kpi.sales_amount  or 0) or 0)
        kpi.option_sales = float(data.get('option_sales',  kpi.option_sales  or 0) or 0)
        kpi.estimated_sales      = float(data.get('estimated_sales',      kpi.estimated_sales      or 0) or 0)
        kpi.target_sales         = float(data.get('target_sales',         kpi.target_sales         or 0) or 0)
        kpi.fire_insurance_count = int(data.get('fire_insurance_count',   kpi.fire_insurance_count or 0) or 0)
        kpi.lifeline_count       = int(data.get('lifeline_count',         kpi.lifeline_count       or 0) or 0)
        kpi.moving_count         = int(data.get('moving_count',           kpi.moving_count         or 0) or 0)
        db.session.commit()
        return jsonify({'status': 'ok', 'id': kpi.id})
    except Exception as e:
        db.session.rollback()
        tb = traceback.format_exc()
        print(f"api_kpi_input error: {e}\n{tb}")
        return jsonify({'error': str(e), 'detail': tb[-300:]}), 500


def _apply_pl_fields(pl, data):
    """PLレコードに詳細フィールドを適用する"""
    for field in ['revenue', 'gross_profit', 'ad_cost', 'labor_cost', 'other_fixed', 'other_variable',
                  'brokerage_fee', 'ad_income', 'lifeline_income', 'moving_income',
                  'fire_insurance_income', 'other_income',
                  'suumo_cost', 'homes_cost', 'athome_cost', 'instagram_cost', 'tiktok_cost',
                  'google_ads_cost', 'line_cost', 'hp_cost', 'meo_cost', 'other_ad_cost',
                  'regular_salary', 'parttime_salary', 'commission_pay',
                  'pl_rent', 'pl_parking', 'pl_copier', 'pl_internet',
                  'pl_consultant', 'pl_insurance', 'pl_cloud']:
        if field in data:
            setattr(pl, field, float(data.get(field, 0) or 0))
    # social_insurance → commission_payカラムにマッピング
    if 'social_insurance' in data:
        pl.commission_pay = float(data.get('social_insurance', 0) or 0)
    # 広告費合計：ad_itemsがある場合はそちらで計算（api_pl_inputで保存後に再計算）
    if 'ad_items' in data:
        ad_items_list = data.get('ad_items') or []
        if isinstance(ad_items_list, str):
            import json as _j
            try: ad_items_list = _j.loads(ad_items_list)
            except: ad_items_list = []
        ad_total = sum(float(i.get('amount', 0) or 0) for i in ad_items_list)
    else:
        ad_total = sum(float(data.get(k, 0) or 0) for k in [
            'suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
            'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost'
        ])
    if ad_total > 0:
        pl.ad_cost = ad_total
    # 人件費合計を自動計算して保存
    labor_total = (float(data.get('regular_salary', 0) or 0) +
                   float(data.get('parttime_salary', 0) or 0) +
                   float(data.get('social_insurance', data.get('commission_pay', 0)) or 0))
    if labor_total > 0:
        pl.labor_cost = labor_total


@app.route("/api/pl/monthly-chart")
@login_required
def api_pl_monthly_chart():
    """経理専用月次グラフ（PLRecordのみ・営業KPIを含まない）"""
    store_id   = request.args.get('store_id', type=int)
    from_param = request.args.get('from')
    to_param   = request.args.get('to')
    today      = date.today()
    allowed_ids = get_allowed_store_ids()
    filter_ids  = [store_id] if (store_id and store_id in allowed_ids) else allowed_ids

    if from_param and to_param:
        try:
            fy, fm = int(from_param[:4]), int(from_param[5:7])
            ty, tm = int(to_param[:4]),   int(to_param[5:7])
            base   = fy * 12 + fm - 1
            end    = ty  * 12 + tm  - 1
            periods = [(t // 12, t % 12 + 1) for t in range(base, end + 1)]
        except Exception:
            periods = None
    else:
        periods = None
    if not periods:
        base = today.year * 12 + today.month - 1
        periods = [(t // 12, t % 12 + 1) for t in range(base - 11, base + 1)]

    result = []
    for y, m in periods:
        pls = PLRecord.query.filter_by(year=y, month=m).filter(PLRecord.store_id.in_(filter_ids)).all()
        total_revenue  = sum(p.revenue or 0 for p in pls)
        total_expenses = 0
        for p in pls:
            cv = PLCustomValue.query.filter_by(store_id=p.store_id, year=y, month=m).all()
            ad_cvs    = [c for c in cv if c.item_type == '広告費']
            fixed_cvs = [c for c in cv if c.item_type == '固定費']
            var_cvs   = [c for c in cv if c.item_type == '変動費']
            ad_t = sum(c.amount for c in ad_cvs) if ad_cvs else (
                (p.ad_cost or 0) or sum(getattr(p, col, 0) or 0 for col in [
                    'suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
                    'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost']))
            lb_t = (p.labor_cost or 0) or ((p.regular_salary or 0)+(p.parttime_salary or 0)+(p.commission_pay or 0))
            ft   = sum(c.amount for c in fixed_cvs)
            vt   = sum(c.amount for c in var_cvs)
            total_expenses += ad_t + lb_t + ft + vt
        result.append({
            'label':    f'{y}/{m:02d}',
            'year':     y,
            'month':    m,
            'revenue':  total_revenue,
            'expenses': total_expenses,
            'profit':   total_revenue - total_expenses,
        })
    return jsonify(result)


@app.route("/api/pl/input", methods=["POST"])
def api_pl_input():
    """PLデータを入力・更新する"""
    data = request.get_json() or request.form
    store_id = safe_store_id(data.get('store_id'))
    if not store_id:
        return jsonify({'error': 'unauthorized'}), 403
    year     = int(data.get('year', current_ym()[0]))
    month    = int(data.get('month', current_ym()[1]))

    pl = PLRecord.query.filter_by(store_id=store_id, year=year, month=month).first()
    if not pl:
        pl = PLRecord(store_id=store_id, year=year, month=month)
        db.session.add(pl)

    _apply_pl_fields(pl, data)
    db.session.commit()

    # 固定費・変動費・カスタム項目を保存（type付き）
    def _save_typed_items(items_json_key, item_type):
        raw = None
        if request.is_json:
            raw = request.get_json().get(items_json_key)
        if raw is None:
            raw = data.get(items_json_key)
        if raw is None: return
        if isinstance(raw, str):
            import json as _json
            try: raw = _json.loads(raw)
            except: return
        # 既存エントリを全削除してから再挿入（ゴースト行防止）
        PLCustomValue.query.filter_by(store_id=store_id, year=year, month=month, item_type=item_type).delete()
        for item in raw:
            name   = str(item.get('name', '')).strip()
            amount = float(item.get('amount', 0) or 0)
            if not name: continue
            cv = PLCustomValue(store_id=store_id, year=year, month=month, item_name=name, item_type=item_type, amount=amount)
            db.session.add(cv)
            # テンプレート登録
            existing = PLCustomItem.query.filter_by(store_id=store_id, name=name, item_type=item_type).first()
            if not existing:
                max_order = db.session.query(db.func.max(PLCustomItem.sort_order)).filter_by(store_id=store_id).scalar() or 0
                db.session.add(PLCustomItem(store_id=store_id, name=name, item_type=item_type, sort_order=max_order + 1))

    _save_typed_items('fixed_items', '固定費')
    _save_typed_items('variable_items', '変動費')
    _save_typed_items('custom_items', 'その他')

    # 広告費カスタム行（送信されている場合は全置換）
    body = request.get_json() if request.is_json else {}
    if 'ad_items' in body:
        PLCustomValue.query.filter_by(
            store_id=store_id, year=year, month=month, item_type='広告費'
        ).delete()
        for item in (body['ad_items'] or []):
            name   = str(item.get('name', '')).strip()
            amount = float(item.get('amount', 0) or 0)
            if not name: continue
            cv = PLCustomValue(store_id=store_id, year=year, month=month,
                               item_name=name, item_type='広告費', amount=amount)
            db.session.add(cv)
        # 固定列をゼロクリア（重複カウント防止）
        for col in ['suumo_cost','homes_cost','athome_cost','instagram_cost','tiktok_cost',
                    'google_ads_cost','line_cost','hp_cost','meo_cost','other_ad_cost']:
            setattr(pl, col, 0)

    db.session.commit()

    return jsonify({'status': 'ok', 'id': pl.id})


@app.route("/api/ad/input", methods=["POST"])
def api_ad_input():
    """広告費を入力・更新する"""
    data = request.get_json() or request.form
    store_id = safe_store_id(data.get('store_id'))
    if not store_id:
        return jsonify({'error': 'unauthorized'}), 403
    source   = data.get('source', '')
    year     = int(data.get('year', current_ym()[0]))
    month    = int(data.get('month', current_ym()[1]))
    cost     = float(data.get('cost', 0))

    ac = AdCost.query.filter_by(store_id=store_id, source=source, year=year, month=month).first()
    if not ac:
        ac = AdCost(store_id=store_id, source=source, year=year, month=month)
        db.session.add(ac)
    ac.cost = cost
    db.session.commit()

    return jsonify({'status': 'ok', 'id': ac.id})


@app.route("/api/store/add", methods=["POST"])
@login_required
def api_store_add():
    """店舗を追加する（ログインユーザーのテナントに所属）"""
    uid = session.get('app_user_id')
    cur_user = AppUser.query.get(uid) if uid else None
    data = request.get_json() or request.form
    store = Store(
        name=data.get('name', ''),
        tenant_id=cur_user.tenant_id if cur_user else None,
        rent=float(data.get('rent', 0)),
        parking_fee=float(data.get('parking_fee', 0)),
        copier_fee=float(data.get('copier_fee', 0)),
        internet_fee=float(data.get('internet_fee', 0)),
        consultant_fee=float(data.get('consultant_fee', 0)),
        insurance_fee=float(data.get('insurance_fee', 0)),
        cloud_fee=float(data.get('cloud_fee', 0)),
        is_active=True,
    )
    db.session.add(store)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': store.id, 'name': store.name})


@app.route("/api/staff", methods=["GET"])
def api_staff_list():
    """スタッフ一覧（テナント分離）"""
    allowed_ids = get_allowed_store_ids()
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    result = [{'id': s.id, 'name': s.name, 'role': s.role} for s in staff_list]
    return jsonify(result)


@app.route("/api/kpi/<int:kpi_id>", methods=["DELETE"])
def api_kpi_delete(kpi_id):
    """KPI削除"""
    kpi = SalesKPI.query.get_or_404(kpi_id)
    db.session.delete(kpi)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/kpi/<int:kpi_id>", methods=["PUT"])
def api_kpi_update(kpi_id):
    """KPI更新"""
    kpi = SalesKPI.query.get_or_404(kpi_id)
    data = request.get_json() or request.form
    kpi.inquiries     = int(data.get('inquiries',     kpi.inquiries))
    kpi.store_visits  = int(data.get('store_visits',  kpi.store_visits))
    kpi.viewings      = int(data.get('viewings',      kpi.viewings))
    kpi.applications  = int(data.get('applications',  kpi.applications))
    kpi.contracts     = int(data.get('contracts',     kpi.contracts))
    kpi.cancellations = int(data.get('cancellations', kpi.cancellations))
    kpi.sales_amount  = float(data.get('sales_amount', kpi.sales_amount))
    kpi.option_sales  = float(data.get('option_sales', kpi.option_sales))
    kpi.estimated_sales     = float(data.get('estimated_sales', kpi.estimated_sales or 0))
    kpi.fire_insurance_count= int(data.get('fire_insurance_count', kpi.fire_insurance_count or 0))
    kpi.lifeline_count      = int(data.get('lifeline_count', kpi.lifeline_count or 0))
    kpi.moving_count        = int(data.get('moving_count', kpi.moving_count or 0))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/lead/<int:lead_id>", methods=["DELETE"])
def api_lead_delete(lead_id):
    """リード削除"""
    lead = Lead.query.get_or_404(lead_id)
    db.session.delete(lead)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/lead/<int:lead_id>", methods=["PUT"])
def api_lead_update(lead_id):
    """リード更新"""
    lead = Lead.query.get_or_404(lead_id)
    data = request.get_json() or request.form
    if 'source' in data or 'media' in data:
        lead.source = data.get('source') or data.get('media', lead.source)
    if 'status' in data:
        lead.status = data.get('status', lead.status)
    if 'customer_name' in data:
        lead.customer_name = data.get('customer_name', lead.customer_name)
    if 'assigned_staff_id' in data or 'assignee_id' in data:
        lead.assigned_staff_id = data.get('assigned_staff_id') or data.get('assignee_id') or lead.assigned_staff_id
    if 'note' in data or 'memo' in data:
        lead.note = data.get('note') or data.get('memo', lead.note)
    if 'line_added' in data:
        lead.line_added = str(data.get('line_added', '0')) in ('1', 'true', 'True', 'on')
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/pl/<int:pl_id>", methods=["DELETE"])
def api_pl_delete(pl_id):
    """PL削除"""
    pl = PLRecord.query.get_or_404(pl_id)
    PLCustomValue.query.filter_by(store_id=pl.store_id, year=pl.year, month=pl.month).delete()
    db.session.delete(pl)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/pl/<int:pl_id>", methods=["PUT"])
def api_pl_update(pl_id):
    """PL更新"""
    pl = PLRecord.query.get_or_404(pl_id)
    data = request.get_json() or request.form
    _apply_pl_fields(pl, data)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/staff/<int:staff_id>", methods=["DELETE"])
def api_staff_delete(staff_id):
    """スタッフ削除（論理削除）"""
    staff = Staff.query.get_or_404(staff_id)
    staff.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/staff/<int:staff_id>", methods=["PUT"])
def api_staff_update(staff_id):
    """スタッフ更新"""
    staff = Staff.query.get_or_404(staff_id)
    data = request.get_json() or request.form
    if 'name' in data:
        staff.name = data.get('name', staff.name)
    if 'role' in data:
        staff.role = data.get('role', staff.role)
    if 'store_id' in data:
        staff.store_id = int(data.get('store_id')) or staff.store_id
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/staff/add", methods=["POST"])
def api_staff_add():
    """スタッフを追加する"""
    data = request.get_json() or request.form
    hired_str = data.get('hired_at', '')
    hired_date = None
    if hired_str:
        try:
            hired_date = date.fromisoformat(hired_str)
        except ValueError:
            pass

    staff = Staff(
        name=data.get('name', ''),
        store_id=int(data.get('store_id', 0)) or None,
        role=data.get('role', '営業'),
        is_active=True,
        hired_at=hired_date,
    )
    db.session.add(staff)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': staff.id, 'name': staff.name})


# ── Excelインポート ──────────────────────────────────────

@app.route("/import-excel")
@login_required
def import_excel_page():
    """Excelインポートページ"""
    # インポート履歴（source_fileでグループ化）
    from sqlalchemy import func
    history = (db.session.query(
        ContractRecord.source_file,
        ContractRecord.year,
        ContractRecord.month,
        func.count(ContractRecord.id).label('count'),
        func.max(ContractRecord.imported_at).label('imported_at'),
    )
    .filter(ContractRecord.source_file != None)
    .group_by(ContractRecord.source_file, ContractRecord.year, ContractRecord.month)
    .order_by(func.max(ContractRecord.imported_at).desc())
    .limit(20)
    .all())

    year, month = current_ym()
    return render_template("import_excel.html",
                           history=history, year=year, month=month,
                           now=datetime.now())


@app.route("/api/import/excel", methods=["POST"])
def api_import_excel():
    """ExcelファイルをインポートしてJSONで結果を返す"""
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルが選択されていません'}), 400

    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'ファイル名が空です'}), 400

    year = request.form.get('year', type=int) or current_ym()[0]
    month = request.form.get('month', type=int) or current_ym()[1]
    store_id = safe_store_id(request.form.get('store_id', type=int))

    filename = secure_filename(f.filename)

    # 一時ファイルに保存してから処理
    suffix = os.path.splitext(filename)[1] or '.xlsx'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        result = import_excel_file(tmp_path, year, month, store_id)
        # ソースファイル名を記録
        with app.app_context():
            ContractRecord.query.filter_by(year=year, month=month).filter(
                ContractRecord.source_file == None
            ).update({'source_file': filename})
            db.session.commit()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    result['filename'] = filename
    result['year'] = year
    result['month'] = month
    return jsonify(result)


# ── 申込台帳 ─────────────────────────────────────────────

@app.route("/contracts")
@login_required
def contracts():
    """申込台帳ページ"""
    _active = get_allowed_store_ids()
    staff_list = Staff.query.filter(Staff.store_id.in_(_active), Staff.is_active == True).all() if _active else []
    year, month = current_ym()
    return render_template("contracts.html",
                           staff_list=staff_list, year=year, month=month,
                           now=datetime.now())


@app.route("/api/contracts")
def api_contracts():
    """ContractRecord一覧JSON (?year=&month=&staff_id=&status=)"""
    year = request.args.get('year', type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    staff_id = request.args.get('staff_id', type=int)
    status = request.args.get('status', '')

    query = ContractRecord.query.filter_by(year=year, month=month)
    if staff_id:
        query = query.filter_by(staff_id=staff_id)
    if status:
        query = query.filter_by(status=status)

    records = query.order_by(ContractRecord.seq_no).all()

    result = []
    for rc in records:
        result.append({
            'id': rc.id,
            'seq_no': rc.seq_no,
            'status': rc.status,
            'staff_name': rc.staff_name_raw,
            'application_date': rc.application_date.isoformat() if rc.application_date else None,
            'property_name': rc.property_name,
            'room_no': rc.room_no,
            'customer_name': rc.customer_name,
            'phone': rc.phone,
            'rent': rc.rent,
            'media': rc.media,
            'management_company': rc.management_company,
            'ad_pct': rc.ad_pct,
            'ad_received': rc.ad_received,
            'lifeline': rc.lifeline,
            'moving': rc.moving,
            'fire_insurance': rc.fire_insurance,
            'contract_amount': rc.contract_amount,
            'cancel_type': rc.cancel_type,
            'review_status': rc.review_status,
            'settlement_date': rc.settlement_date.isoformat() if rc.settlement_date else None,
            'ad_income_date': rc.ad_income_date.isoformat() if rc.ad_income_date else None,
            'ad_income_date_raw': rc.ad_income_date_raw,
        })

    return jsonify({
        'year': year,
        'month': month,
        'records': result,
        'total': len(result),
        'total_contract_amount': sum(r['contract_amount'] or 0 for r in result),
        'contracts': sum(1 for r in result if r['status'] == '契約'),
        'applications': sum(1 for r in result if r['status'] == '申込'),
        'cancellations': sum(1 for r in result if r['status'] == 'キャンセル'),
    })


@app.route("/api/contracts/<int:contract_id>", methods=["DELETE"])
def api_contract_delete(contract_id):
    """ContractRecord削除"""
    rc = ContractRecord.query.get_or_404(contract_id)
    db.session.delete(rc)
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── 入金管理 ─────────────────────────────────────────────

@app.route("/payments")
@login_required
def payments():
    """入金管理ページ"""
    _allowed = get_allowed_store_ids()
    staff_list = Staff.query.filter(Staff.store_id.in_(_allowed), Staff.is_active == True).all() if _allowed else []
    year, month = current_ym()
    return render_template("payments.html",
                           staff_list=staff_list, year=year, month=month,
                           now=datetime.now())


@app.route("/api/payments/summary")
def api_payments_summary():
    """月次入金サマリーJSON (?year=&month=)"""
    year = request.args.get('year', type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]

    contracts = ContractRecord.query.filter_by(year=year, month=month, status='契約').all()

    # 仲介手数料予定額（賃料 × 手数料% または賃料1ヶ月分）
    commission_total = 0
    ad_income_total = 0
    ad_received_total = 0
    ad_unreceived_total = 0
    option_total = 0

    for rc in contracts:
        rent = rc.rent or 0
        commission_pct = rc.commission_pct or 100
        ad_pct = rc.ad_pct or 0

        commission = rent * (commission_pct / 100)
        commission_total += commission

        ad_amount = rent * (ad_pct / 100)
        ad_income_total += ad_amount

        if rc.ad_received and '○' in rc.ad_received:
            ad_received_total += ad_amount
        else:
            ad_unreceived_total += ad_amount

        if rc.lifeline and '○' in rc.lifeline:
            option_total += 5000
        if rc.moving and '○' in rc.moving:
            option_total += 3000

    # 入金状況詳細リスト
    details = []
    for rc in contracts:
        rent = rc.rent or 0
        ad_pct = rc.ad_pct or 0
        commission_pct = rc.commission_pct or 100
        ad_amount = rent * (ad_pct / 100)
        commission = rent * (commission_pct / 100)
        details.append({
            'id': rc.id,
            'customer_name': rc.customer_name,
            'property_name': rc.property_name,
            'room_no': rc.room_no,
            'staff_name': rc.staff_name_raw,
            'rent': rent,
            'ad_pct': ad_pct,
            'ad_amount': round(ad_amount),
            'ad_received': rc.ad_received,
            'ad_income_date': rc.ad_income_date.isoformat() if rc.ad_income_date else None,
            'ad_income_date_raw': rc.ad_income_date_raw,
            'commission': round(commission),
            'settlement_date': rc.settlement_date.isoformat() if rc.settlement_date else None,
            'contract_amount': rc.contract_amount or 0,
        })

    # 月別入金推移（過去6ヶ月）
    monthly_trend = []
    for i in range(5, -1, -1):
        t = date.today().replace(day=1)
        for _ in range(i):
            t = (t - timedelta(days=1)).replace(day=1)
        y, m = t.year, t.month
        recs = ContractRecord.query.filter_by(year=y, month=m, status='契約').all()
        received = 0
        unreceived = 0
        for rc in recs:
            rent = rc.rent or 0
            ad_pct_v = rc.ad_pct or 0
            ad_amt = rent * (ad_pct_v / 100)
            if rc.ad_received and '○' in rc.ad_received:
                received += ad_amt
            else:
                unreceived += ad_amt
        monthly_trend.append({
            'label': f'{y}/{m:02d}',
            'received': round(received),
            'unreceived': round(unreceived),
        })

    return jsonify({
        'year': year,
        'month': month,
        'commission_total': round(commission_total),
        'ad_income_total': round(ad_income_total),
        'ad_received_total': round(ad_received_total),
        'ad_unreceived_total': round(ad_unreceived_total),
        'option_total': round(option_total),
        'contract_count': len(contracts),
        'details': details,
        'monthly_trend': monthly_trend,
    })


# ── 設定ページ ───────────────────────────────────────────

@app.route("/settings")
@login_required
def settings():
    """設定ページ（スタッフ以上）"""
    stores     = get_allowed_stores(ignore_active=True)
    # スタッフ表示はアクティブ店舗のみ（店舗ごとに分離）
    active_store_ids = get_allowed_store_ids(ignore_active=False)
    staff_list = Staff.query.filter(Staff.store_id.in_(active_store_ids), Staff.is_active == True).order_by(Staff.name).all() if active_store_ids else []
    # 担当店舗名マップ
    store_name_map = {s.id: s.name for s in stores}
    # アカウント一覧はオーナー・店長のみ表示（自テナントのみ）
    user = AppUser.query.get(session['app_user_id'])
    is_owner = user and user.role == 'owner'
    is_manager = user and user.role == 'store_manager'
    if (is_owner or is_manager) and user.tenant_id:
        accounts = AppUser.query.filter_by(is_active=True, tenant_id=user.tenant_id).all()
    else:
        accounts = []
    return render_template("settings.html",
                           stores=stores, staff_list=staff_list, accounts=accounts,
                           store_name_map=store_name_map,
                           is_owner=is_owner, is_manager=is_manager,
                           current_user=user,
                           now=datetime.now())


@app.route("/api/settings/staff/add", methods=["POST"])
@login_required
def api_settings_staff_add():
    """スタッフ追加"""
    data = request.get_json() or request.form
    # ignore_active=True で設定ページと同じ基準で店舗を解決する
    allowed = get_allowed_store_ids()
    if not allowed:
        return jsonify({'error': 'store not found'}), 403
    try:
        req_sid = int(data.get('store_id') or 0)
    except (TypeError, ValueError):
        req_sid = 0
    sid = req_sid if req_sid and req_sid in allowed else allowed[0]

    hired_str = data.get('hired_at', '')
    hired_date = None
    if hired_str:
        try:
            hired_date = date.fromisoformat(hired_str)
        except ValueError:
            pass
    staff = Staff(
        name=data.get('name', ''),
        store_id=sid,
        role=data.get('role', '営業'),
        is_active=True,
        hired_at=hired_date,
    )
    db.session.add(staff)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': staff.id, 'name': staff.name})


@app.route("/api/settings/staff/<int:staff_id>", methods=["PUT"])
@login_required
def api_settings_staff_update(staff_id):
    """スタッフ更新"""
    staff = Staff.query.get_or_404(staff_id)
    data = request.get_json() or request.form
    if 'name' in data:
        staff.name = data.get('name')
    if 'role' in data:
        staff.role = data.get('role')
    if 'hired_at' in data:
        try:
            staff.hired_at = date.fromisoformat(data['hired_at']) if data['hired_at'] else None
        except ValueError:
            pass
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/settings/staff/<int:staff_id>", methods=["DELETE"])
@login_required
def api_settings_staff_delete(staff_id):
    """スタッフ削除（論理削除）"""
    staff = Staff.query.get_or_404(staff_id)
    staff.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/settings/account/add", methods=["POST"])
@owner_or_manager_required
def api_settings_account_add():
    """アカウント追加（オーナーの自テナントに所属させる）"""
    data = request.get_json() or request.form
    cur = AppUser.query.get(session['app_user_id'])
    if not cur or not cur.tenant_id:
        return jsonify({'status': 'error', 'message': 'テナント情報が取得できません'}), 403
    if AppUser.query.filter_by(username=data.get('username', ''), is_active=True).first():
        return jsonify({'status': 'error', 'message': 'そのユーザー名は既に使用されています'}), 400
    # store_id: リクエストで指定されていればそれを使い、なければオーナーの最初の店舗
    sid = safe_store_id(data.get('store_id'))
    user = AppUser(
        tenant_id=cur.tenant_id,
        username=data.get('username', ''),
        password_hash=generate_password_hash(data.get('password', '')),
        role=data.get('role', 'staff'),
        staff_id=int(data.get('staff_id')) if data.get('staff_id') else None,
        store_id=sid,
        is_active=True,
        can_view_accounting=bool(data.get('can_view_accounting', True)),
        can_view_all_staff=bool(data.get('can_view_all_staff', True)),
        can_edit_kpi=bool(data.get('can_edit_kpi', True)),
        can_manage_uncollected=bool(data.get('can_manage_uncollected', True)),
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': user.id})


@app.route("/api/settings/account/<int:account_id>/password", methods=["PUT"])
@owner_or_manager_required
def api_settings_account_password(account_id):
    """パスワード変更（オーナーが他ユーザーのパスワードを変更）"""
    user = AppUser.query.get_or_404(account_id)
    data = request.get_json() or request.form
    new_password = data.get('password', '')
    if not new_password:
        return jsonify({'status': 'error', 'message': 'パスワードを入力してください'}), 400
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/settings/account/<int:account_id>", methods=["DELETE"])
@owner_or_manager_required
def api_settings_account_delete(account_id):
    """アカウント削除"""
    user = AppUser.query.get_or_404(account_id)
    if user.username == 'owner':
        return jsonify({'status': 'error', 'message': 'オーナーアカウントは削除できません'}), 400
    db.session.delete(user)   # 完全削除（ログなし）
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/settings/password", methods=["POST"])
@login_required
def api_settings_my_password():
    """自分のパスワード変更"""
    data = request.get_json() or request.form
    user = AppUser.query.get(session['app_user_id'])
    if not user:
        return jsonify({'status': 'error', 'message': 'ユーザーが見つかりません'}), 404
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    if not check_password_hash(user.password_hash, current_password):
        return jsonify({'status': 'error', 'message': '現在のパスワードが正しくありません'}), 400
    if not new_password:
        return jsonify({'status': 'error', 'message': '新パスワードを入力してください'}), 400
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/settings/profile", methods=["PUT"])
@login_required
def api_settings_profile():
    """ログイン中ユーザーの表示名（username）変更"""
    user = AppUser.query.get_or_404(session['user_id'])
    data = request.get_json() or request.form
    new_name = (data.get('display_name') or '').strip()
    if not new_name:
        return jsonify({'status': 'error', 'message': '名前を入力してください'}), 400
    if AppUser.query.filter(AppUser.username == new_name, AppUser.id != user.id).first():
        return jsonify({'status': 'error', 'message': 'その名前は既に使用されています'}), 400
    user.username = new_name
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── スタッフ別6ヶ月推移 API ──────────────────────────────────────
def _staff_real_stats(filter_ids, staff_id, y, m):
    """指定スタッフ・年月の実績を各管理表から集計（反響/接客/申込/売上）"""
    import calendar as _cal
    m_start = date(y, m, 1)
    m_end   = date(y, m, _cal.monthrange(y, m)[1])
    if not filter_ids:
        return {'inquiries': 0, 'visits': 0, 'applications': 0, 'revenue': 0}

    eq = EchoRecord.query.filter(EchoRecord.store_id.in_(filter_ids),
                                 EchoRecord.echo_date >= m_start, EchoRecord.echo_date <= m_end)
    cq = CustomerServiceRecord.query.filter(CustomerServiceRecord.store_id.in_(filter_ids),
                                            CustomerServiceRecord.service_date >= m_start,
                                            CustomerServiceRecord.service_date <= m_end)
    aq = ApplicationRecord.query.filter(ApplicationRecord.store_id.in_(filter_ids),
                                        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
                                        db.extract('year',  ApplicationRecord.application_date) == y,
                                        db.extract('month', ApplicationRecord.application_date) == m)
    if staff_id:
        eq = eq.filter(EchoRecord.staff_id == staff_id)
        cq = cq.filter(CustomerServiceRecord.staff_id == staff_id)
        aq = aq.filter(ApplicationRecord.staff_id == staff_id)
    apps = aq.all()
    return {
        'inquiries':    eq.count(),
        'visits':       cq.count(),
        'applications': len(apps),
        'revenue':      sum(_record_approved_amount(a) for a in apps),
    }


@app.route("/api/kpi/staff-history")
def api_kpi_staff_history():
    """スタッフ別の6ヶ月推移データを返す"""
    staff_id = request.args.get('staff_id', type=int)
    store_id = request.args.get('store_id', type=int)
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]

    # 店舗フィルタ（テナント分離）
    allowed_ids = get_allowed_store_ids()
    filter_ids = [store_id] if (store_id and store_id in allowed_ids) else allowed_ids

    history = []
    yoy_data = {}
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m < 1:
            m += 12; y -= 1
        query = SalesKPI.query.filter_by(year=y, month=m)
        if filter_ids:
            query = query.filter(SalesKPI.store_id.in_(filter_ids))
        if staff_id:
            query = query.filter_by(staff_id=staff_id)
        kpis = query.all()
        history.append({
            'label':        f'{y}/{m:02d}',
            'contracts':    sum(k.contracts    or 0 for k in kpis),
            'applications': sum(k.applications or 0 for k in kpis),
            'revenue':      sum(k.sales_amount or 0 for k in kpis),
            'inquiries':    sum(k.inquiries    or 0 for k in kpis),
        })
        # 現在月のみ前年同月比を計算（実データから：反響=反響管理表/接客=接客管理表/申込・売上=顧客管理表）
        if i == 0:
            cur  = _staff_real_stats(filter_ids, staff_id, y, m)
            prev = _staff_real_stats(filter_ids, staff_id, y - 1, m)
            yoy_data = {
                'inquiries':    {'cur': cur['inquiries'],    'yoy': prev['inquiries']},
                'visits':       {'cur': cur['visits'],       'yoy': prev['visits']},
                'applications': {'cur': cur['applications'], 'yoy': prev['applications']},
                'revenue':      {'cur': cur['revenue'],      'yoy': prev['revenue']},
            }

    return jsonify({'history': history, 'yoy': yoy_data})


# ── 未入金（AD）管理 API ──────────────────────────────────────────
@app.route("/api/uncollected")
@login_required
def api_uncollected_list():
    store_id = request.args.get('store_id', type=int)
    staff_id = request.args.get('staff_id', type=int)
    include_paid = request.args.get('include_paid', 'false').lower() == 'true'

    q = UncollectedPayment.query
    if store_id:
        q = q.filter_by(store_id=store_id)
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    if not include_paid:
        q = q.filter_by(is_paid=False)

    items = q.order_by(UncollectedPayment.expected_payment_date.asc()).all()

    def fmt_date(d): return d.strftime('%Y-%m-%d') if d else None

    result = []
    for p in items:
        staff = Staff.query.get(p.staff_id) if p.staff_id else None
        result.append({
            'id':                   p.id,
            'store_id':             p.store_id,
            'staff_id':             p.staff_id,
            'staff_name':           staff.name if staff else '',
            'property_name':        p.property_name or '',
            'room_number':          p.room_number or '',
            'application_date':     fmt_date(p.application_date),
            'management_company':   p.management_company or '',
            'customer_name':        p.customer_name or '',
            'expected_payment_date':fmt_date(p.expected_payment_date),
            'amount':               p.amount or 0,
            'memo':                 p.memo or '',
            'is_paid':              p.is_paid,
        })
    return jsonify(result)


@app.route("/api/uncollected/paid-sum")
@login_required
def api_uncollected_paid_sum():
    """指定年月に入金済みとなった未入金データの合計額を返す"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    allowed = get_allowed_store_ids(ignore_active=True)

    from datetime import date as _date
    month_start = _date(year, month, 1)
    if month == 12:
        month_end = _date(year + 1, 1, 1)
    else:
        month_end = _date(year, month + 1, 1)

    q = UncollectedPayment.query.filter(
        UncollectedPayment.is_paid == True,
        UncollectedPayment.expected_payment_date >= month_start,
        UncollectedPayment.expected_payment_date <  month_end,
    )
    if allowed:
        q = q.filter(UncollectedPayment.store_id.in_(allowed))
    total = sum((p.amount or 0) for p in q.all())
    return jsonify({'total': total, 'year': year, 'month': month})


@app.route("/api/uncollected", methods=["POST"])
@login_required
def api_uncollected_add():
    data = request.get_json() or request.form
    from datetime import datetime as _dt
    def parse_date(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    p = UncollectedPayment(
        store_id=safe_store_id(data.get('store_id')) or (get_allowed_store_ids() or [1])[0],
        staff_id=int(data.get('staff_id') or 0) or None,
        property_name=data.get('property_name', ''),
        room_number=data.get('room_number', ''),
        application_date=parse_date(data.get('application_date')),
        management_company=data.get('management_company', ''),
        customer_name=data.get('customer_name', ''),
        expected_payment_date=parse_date(data.get('expected_payment_date')),
        amount=float(data.get('amount', 0) or 0),
        memo=data.get('memo', ''),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': p.id})


@app.route("/api/uncollected/<int:pid>", methods=["PUT"])
@login_required
def api_uncollected_update(pid):
    p = UncollectedPayment.query.get_or_404(pid)
    data = request.get_json() or request.form
    from datetime import datetime as _dt
    def parse_date(s):
        if not s: return None
        try: return _dt.strptime(s, '%Y-%m-%d').date()
        except: return None

    if 'property_name'      in data: p.property_name      = data['property_name']
    if 'room_number'        in data: p.room_number        = data['room_number']
    if 'application_date'   in data: p.application_date   = parse_date(data['application_date'])
    if 'management_company' in data: p.management_company = data['management_company']
    if 'customer_name'      in data: p.customer_name      = data['customer_name']
    if 'expected_payment_date' in data: p.expected_payment_date = parse_date(data['expected_payment_date'])
    if 'amount'  in data: p.amount   = float(data['amount'] or 0)
    if 'memo'    in data: p.memo     = data['memo']
    if 'staff_id'in data: p.staff_id = int(data['staff_id'] or 0) or None
    if 'is_paid' in data: p.is_paid  = bool(data['is_paid'])
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/uncollected/<int:pid>", methods=["DELETE"])
@login_required
def api_uncollected_delete(pid):
    p = UncollectedPayment.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/uncollected/<int:pid>/paid", methods=["POST"])
@login_required
def api_uncollected_paid(pid):
    p = UncollectedPayment.query.get_or_404(pid)
    if not p.is_paid:
        p.is_paid = True
        # 対応スタッフのSalesKPIに売上を加算
        if p.staff_id and p.amount:
            data = request.get_json() or {}
            req_year  = data.get('year')
            req_month = data.get('month')
            if req_year and req_month:
                ref_year, ref_month = int(req_year), int(req_month)
            else:
                ref_date = p.application_date or p.expected_payment_date or date.today()
                ref_year, ref_month = ref_date.year, ref_date.month
            kpi = SalesKPI.query.filter_by(
                staff_id=p.staff_id, store_id=p.store_id,
                year=ref_year, month=ref_month
            ).first()
            if not kpi:
                kpi = SalesKPI(staff_id=p.staff_id, store_id=p.store_id,
                               year=ref_year, month=ref_month)
                db.session.add(kpi)
            kpi.sales_amount = (kpi.sales_amount or 0) + float(p.amount)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/uncollected/sync-from-applications", methods=["POST"])
@login_required
def api_uncollected_sync_from_applications():
    """前月の申込一覧で未承認の案件を未入金一覧に自動転記（重複スキップ）"""
    data = request.get_json() or {}
    year  = data.get('year')
    month = data.get('month')
    store_id_param = data.get('store_id')

    if not year or not month:
        return jsonify({'added': 0})

    year, month = int(year), int(month)
    allowed_ids = get_allowed_store_ids()

    if store_id_param and int(store_id_param) in allowed_ids:
        filter_ids = [int(store_id_param)]
    else:
        filter_ids = allowed_ids

    if not filter_ids:
        return jsonify({'added': 0})

    # 対象月の申込で未承認のもの（キャンセル除く）
    from sqlalchemy import extract
    apps = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(filter_ids),
        extract('year',  ApplicationRecord.application_date) == year,
        extract('month', ApplicationRecord.application_date) == month,
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
        db.or_(
            db.and_(ApplicationRecord.ad_amount != 0,       ApplicationRecord.ad_approved == False),
            db.and_(ApplicationRecord.brokerage_fee != 0,   ApplicationRecord.brokerage_approved == False),
        )
    ).all()

    added = 0
    for rec in apps:
        pending_amount = 0
        if (rec.ad_amount or 0) != 0 and not rec.ad_approved:
            pending_amount += rec.ad_amount or 0
        if (rec.brokerage_fee or 0) != 0 and not rec.brokerage_approved:
            pending_amount += rec.brokerage_fee or 0
        pending_amount += rec.option_amount or 0
        if pending_amount <= 0:
            continue

        # 同一案件の重複チェック（物件名＋顧客名＋申込日＋店舗）
        exists = UncollectedPayment.query.filter_by(
            store_id=rec.store_id,
            property_name=rec.property_name,
            customer_name=rec.customer_name,
            application_date=rec.application_date,
        ).first()
        if exists:
            continue

        up = UncollectedPayment(
            store_id=rec.store_id,
            staff_id=rec.staff_id,
            property_name=rec.property_name,
            room_number=rec.room_number,
            application_date=rec.application_date,
            customer_name=rec.customer_name,
            amount=pending_amount,
            memo='申込一覧から自動転記',
            is_paid=False,
        )
        db.session.add(up)
        added += 1

    db.session.commit()
    return jsonify({'added': added})


# ── 有給管理 ──────────────────────────────────────────────

@app.route("/leave-management")
@login_required
@manager_or_above_required
def leave_management():
    stores     = get_allowed_stores(ignore_active=True)   # サイドバー用
    active_ids = get_allowed_store_ids()                   # アクティブ店舗のみ
    staff_list = Staff.query.filter(
        Staff.store_id.in_(active_ids), Staff.is_active == True
    ).order_by(Staff.name).all()
    year = request.args.get('year', type=int) or date.today().year
    return render_template("leave_management.html", stores=stores, staff_list=staff_list, year=year)


@app.route("/api/set-active-store", methods=["POST"])
@login_required
def api_set_active_store():
    """プレミアオーナーがサイドバーで店舗を切り替えるためのAPI"""
    data = request.get_json() or {}
    store_id = data.get('store_id')
    allowed = get_allowed_store_ids(ignore_active=True)  # 全店舗で検証
    if store_id and int(store_id) in allowed:
        session['active_store_id'] = int(store_id)
    else:
        session.pop('active_store_id', None)
    return jsonify({'status': 'ok', 'active_store_id': session.get('active_store_id')})


@app.route("/api/leave", methods=["GET"])
@login_required
def api_leave_list():
    year     = request.args.get('year',     type=int) or date.today().year
    staff_id = request.args.get('staff_id', type=int)
    # アクティブ店舗のスタッフのみ対象
    _allowed_store_ids = get_allowed_store_ids()
    _allowed_staff_ids = [s.id for s in Staff.query.filter(
        Staff.store_id.in_(_allowed_store_ids), Staff.is_active == True
    ).all()] if _allowed_store_ids else []
    q = LeaveRecord.query.filter(
        db.extract('year', LeaveRecord.leave_date) == year,
        LeaveRecord.staff_id.in_(_allowed_staff_ids) if _allowed_staff_ids else db.false()
    )
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    records = q.order_by(LeaveRecord.leave_date.desc()).all()
    _allowed = get_allowed_store_ids()
    staff_map = {s.id: s.name for s in Staff.query.filter(Staff.store_id.in_(_allowed)).all() if _allowed}
    return jsonify([{
        'id':         r.id,
        'staff_id':   r.staff_id,
        'staff_name': staff_map.get(r.staff_id, '?'),
        'leave_date': r.leave_date.strftime('%Y-%m-%d'),
        'leave_type': r.leave_type,
        'days':       r.days,
        'memo':       r.memo or '',
        'status':     r.status,
    } for r in records])


@app.route("/api/leave", methods=["POST"])
@login_required
def api_leave_create():
    data = request.get_json() or {}
    try:
        ld = datetime.strptime(data['leave_date'], '%Y-%m-%d').date()
    except Exception:
        return jsonify({'error': '日付が不正です'}), 400
    r = LeaveRecord(
        staff_id   = data.get('staff_id'),
        leave_date = ld,
        leave_type = data.get('leave_type', '有給'),
        days       = float(data.get('days', 1.0)),
        memo       = data.get('memo', ''),
        status     = data.get('status', '承認済'),
    )
    db.session.add(r)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': r.id})


@app.route("/api/leave/<int:lid>", methods=["PUT"])
@login_required
def api_leave_update(lid):
    r = LeaveRecord.query.get_or_404(lid)
    data = request.get_json() or {}
    if 'leave_date' in data:
        r.leave_date = datetime.strptime(data['leave_date'], '%Y-%m-%d').date()
    if 'leave_type' in data: r.leave_type = data['leave_type']
    if 'days'       in data: r.days       = float(data['days'])
    if 'memo'       in data: r.memo       = data['memo']
    if 'status'     in data: r.status     = data['status']
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/leave/<int:lid>", methods=["DELETE"])
@login_required
def api_leave_delete(lid):
    r = LeaveRecord.query.get_or_404(lid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/leave/balance", methods=["GET"])
@login_required
def api_leave_balance():
    """スタッフ別有給残日数サマリー"""
    year = request.args.get('year', type=int) or date.today().year
    # 店舗フィルタ（テナント分離）
    _allowed = get_allowed_store_ids()
    staff_list = Staff.query.filter(
        Staff.store_id.in_(_allowed), Staff.is_active == True
    ).all() if _allowed else []
    result = []
    for s in staff_list:
        bal = LeaveBalance.query.filter_by(staff_id=s.id, year=year).first()
        total = bal.total_days if bal else 10.0
        used = db.session.query(db.func.sum(LeaveRecord.days)).filter(
            LeaveRecord.staff_id == s.id,
            db.extract('year', LeaveRecord.leave_date) == year,
            LeaveRecord.leave_type == '有給',
            LeaveRecord.status != '却下'
        ).scalar() or 0
        result.append({
            'staff_id':   s.id,
            'staff_name': s.name,
            'total_days': total,
            'used_days':  used,
            'remain_days': total - used,
        })
    return jsonify(result)


@app.route("/api/leave/balance", methods=["POST"])
@login_required
def api_leave_balance_update():
    """有給付与日数更新"""
    data = request.get_json() or {}
    staff_id = data.get('staff_id')
    year     = data.get('year') or date.today().year
    total    = float(data.get('total_days', 10))
    bal = LeaveBalance.query.filter_by(staff_id=staff_id, year=year).first()
    if bal:
        bal.total_days = total
    else:
        bal = LeaveBalance(staff_id=staff_id, year=year, total_days=total)
        db.session.add(bal)
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── 日報 ──────────────────────────────────────────────────

@app.route("/daily-report")
@login_required
@block_super_admin
def daily_report():
    """日報ページ"""
    allowed_stores = get_allowed_stores()
    allowed_ids = [s.id for s in allowed_stores]
    stores = allowed_stores
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    year, month = current_ym()
    today = date.today()
    # デフォルトタスクが未作成なら初期化（テナント分離）
    store_id = allowed_ids[0] if allowed_ids else None
    if store_id:
        default_tasks = [
            ("来店前日連絡", True, 1),
            ("来店当日連絡", True, 2),
            ("申込管理入力", True, 3),
        ]
        for task_name, is_def, order in default_tasks:
            exists = DailyTaskTemplate.query.filter_by(store_id=store_id, task_name=task_name).first()
            if not exists:
                db.session.add(DailyTaskTemplate(
                    store_id=store_id, task_name=task_name,
                    is_default=is_def, is_active=True, sort_order=order
                ))
        db.session.commit()
    tasks = DailyTaskTemplate.query.filter(
        DailyTaskTemplate.store_id.in_(allowed_ids),
        DailyTaskTemplate.is_active == True
    ).order_by(DailyTaskTemplate.sort_order).all()
    return render_template("daily_report.html",
                           stores=stores, staff_list=staff_list,
                           tasks=tasks, year=year, month=month,
                           store_id=store_id, today=today, now=datetime.now())


@app.route("/api/daily-report")
@login_required
def api_daily_report_list():
    """日報一覧取得"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    staff_id = request.args.get('staff_id', type=int)
    report_date_str = request.args.get('date')

    allowed_ids = get_allowed_store_ids()
    q = DailyReport.query
    if allowed_ids:
        q = q.filter(DailyReport.store_id.in_(allowed_ids))
    if report_date_str:
        try:
            rd = datetime.strptime(report_date_str, '%Y-%m-%d').date()
            q = q.filter_by(report_date=rd)
        except Exception:
            pass
    else:
        from calendar import monthrange
        first = date(year, month, 1)
        last  = date(year, month, monthrange(year, month)[1])
        q = q.filter(DailyReport.report_date >= first, DailyReport.report_date <= last)
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    reports = q.order_by(DailyReport.report_date.desc()).all()

    result = []
    for r in reports:
        staff = Staff.query.get(r.staff_id)
        customers = DailyReportCustomer.query.filter_by(report_id=r.id).all()
        task_checks = DailyTaskCheck.query.filter_by(report_id=r.id).all()
        result.append({
            'id': r.id,
            'staff_id': r.staff_id,
            'staff_name': staff.name if staff else '不明',
            'report_date': r.report_date.isoformat(),
            'prev_day_contact_done': r.prev_day_contact_done,
            'same_day_contact_done': r.same_day_contact_done,
            'application_input_done': r.application_input_done,
            'application_count': r.application_count or 0,
            'tomorrow_appointments': r.tomorrow_appointments or '',
            'memo': r.memo or '',
            'customers': [
                {
                    'id': c.id,
                    'customer_name': c.customer_name,
                    'applied': c.applied,
                    'no_apply_reason': c.no_apply_reason or '',
                    'improvement': c.improvement or '',
                } for c in customers
            ],
            'task_checks': {tc.task_id: tc.checked for tc in task_checks},
        })
    return jsonify(result)


@app.route("/api/daily-report", methods=["POST"])
@login_required
def api_daily_report_save():
    """日報保存（新規 or 更新）"""
    data = request.get_json() or {}
    staff_id = int(data.get('staff_id') or 0) or None
    store_id = safe_store_id(data.get('store_id'))
    if not store_id:
        return jsonify({'error': 'unauthorized'}), 403
    report_date_str = data.get('report_date') or date.today().isoformat()
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except Exception:
        report_date = date.today()

    # 同日・同スタッフの日報があれば更新、なければ作成
    report = DailyReport.query.filter_by(staff_id=staff_id, report_date=report_date).first()
    if not report:
        report = DailyReport(staff_id=staff_id, store_id=store_id, report_date=report_date)
        db.session.add(report)

    report.prev_day_contact_done  = bool(data.get('prev_day_contact_done'))
    report.same_day_contact_done  = bool(data.get('same_day_contact_done'))
    report.application_input_done = bool(data.get('application_input_done'))
    report.application_count      = int(data.get('application_count') or 0)
    report.tomorrow_appointments  = data.get('tomorrow_appointments', '')
    report.memo                   = data.get('memo', '')
    report.updated_at             = datetime.utcnow()
    db.session.flush()

    # 接客記録: 全削除 → 再登録
    DailyReportCustomer.query.filter_by(report_id=report.id).delete()
    for c in (data.get('customers') or []):
        if c.get('customer_name', '').strip():
            db.session.add(DailyReportCustomer(
                report_id=report.id,
                customer_name=c['customer_name'].strip(),
                applied=bool(c.get('applied')),
                no_apply_reason=c.get('no_apply_reason', ''),
                improvement=c.get('improvement', ''),
            ))

    # カスタムタスクチェック
    DailyTaskCheck.query.filter_by(report_id=report.id).delete()
    for task_id_str, checked in (data.get('task_checks') or {}).items():
        try:
            db.session.add(DailyTaskCheck(
                report_id=report.id,
                task_id=int(task_id_str),
                checked=bool(checked),
            ))
        except Exception:
            pass

    db.session.commit()
    return jsonify({'status': 'ok', 'id': report.id})


@app.route("/api/daily-report/<int:rid>", methods=["DELETE"])
@login_required
def api_daily_report_delete(rid):
    r = DailyReport.query.get_or_404(rid)
    DailyReportCustomer.query.filter_by(report_id=rid).delete()
    DailyTaskCheck.query.filter_by(report_id=rid).delete()
    db.session.delete(r)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/daily-task-template")
@login_required
def api_task_template_list():
    """タスクテンプレート一覧"""
    store_id = safe_store_id(request.args.get('store_id', type=int))
    if not store_id:
        return jsonify([])
    allowed_ids = get_allowed_store_ids()
    tasks = DailyTaskTemplate.query.filter(
        DailyTaskTemplate.store_id.in_(allowed_ids),
        DailyTaskTemplate.is_active == True
    ).order_by(DailyTaskTemplate.sort_order).all()
    return jsonify([{'id': t.id, 'task_name': t.task_name, 'is_default': t.is_default} for t in tasks])


@app.route("/api/daily-task-template", methods=["POST"])
@login_required
def api_task_template_add():
    """タスクテンプレート追加"""
    data = request.get_json() or {}
    task_name = (data.get('task_name') or '').strip()
    if not task_name:
        return jsonify({'error': 'task_name required'}), 400
    store_id = safe_store_id(data.get('store_id'))
    if not store_id:
        return jsonify({'error': 'unauthorized'}), 403
    max_order = db.session.query(db.func.max(DailyTaskTemplate.sort_order)).filter_by(
        store_id=store_id).scalar() or 0
    t = DailyTaskTemplate(store_id=store_id, task_name=task_name, sort_order=max_order + 1)
    db.session.add(t)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': t.id})


@app.route("/api/daily-task-template/<int:tid>", methods=["PUT"])
@login_required
def api_task_template_update(tid):
    """タスクテンプレート名変更（デフォルト含む）"""
    t = DailyTaskTemplate.query.get_or_404(tid)
    data = request.get_json() or {}
    new_name = (data.get('task_name') or '').strip()
    if not new_name:
        return jsonify({'error': 'task_name required'}), 400
    t.task_name = new_name
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/daily-task-template/<int:tid>", methods=["DELETE"])
@login_required
def api_task_template_delete(tid):
    """タスクテンプレート削除（デフォルトは削除不可）"""
    t = DailyTaskTemplate.query.get_or_404(tid)
    if t.is_default:
        return jsonify({'error': 'デフォルトタスクは削除できません'}), 400
    t.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── パスワードリセット ──────────────────────────────────────

import secrets
from itsdangerous import URLSafeTimedSerializer

def _reset_serializer():
    return URLSafeTimedSerializer(app.secret_key)


def _send_reset_email(to_email, reset_url):
    """パスワードリセットメール送信（Resend API / HTTPS）"""
    resend_key = os.getenv('RESEND_API_KEY', '')
    # Resendドメイン未認証の場合はonboarding@resend.devを使用
    # 独自ドメイン取得後は MAIL_FROM 環境変数で変更可能
    from_email = os.getenv('MAIL_FROM', 'onboarding@resend.dev')
    from_name  = 'ミエルーム'

    if not resend_key:
        app.logger.warning('RESEND_API_KEY が未設定')
        return False

    try:
        import urllib.request, json as _json
        body_html = f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;">
  <div style="text-align:center;margin-bottom:24px;">
    <h2 style="color:#0D9488;margin:0;">ミエルーム</h2>
    <p style="color:#6b7280;font-size:14px;margin:4px 0 0;">不動産賃貸仲介業務管理システム</p>
  </div>
  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:32px;">
    <h3 style="color:#111827;margin:0 0 12px;">パスワードリセットのご案内</h3>
    <p style="color:#374151;line-height:1.7;">以下のボタンからパスワードをリセットしてください。<br>このリンクの有効期限は<strong>1時間</strong>です。</p>
    <div style="text-align:center;margin:28px 0;">
      <a href="{reset_url}" style="background:#0D9488;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;">パスワードをリセットする</a>
    </div>
    <p style="color:#6b7280;font-size:13px;">ボタンが押せない場合は以下のURLをコピーしてください：<br>
    <a href="{reset_url}" style="color:#0D9488;word-break:break-all;">{reset_url}</a></p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
    <p style="color:#9ca3af;font-size:12px;text-align:center;">このメールに心当たりがない場合は無視してください。</p>
  </div>
</div>"""

        payload = _json.dumps({
            'from': f'{from_name} <{from_email}>',
            'to':   [to_email],
            'subject': 'パスワードリセットのご案内 - ミエルーム',
            'html': body_html,
        }).encode('utf-8')

        import ssl
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            'https://api.resend.com/emails',
            data=payload,
            headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'mieroom-app/1.0',
                'Accept': 'application/json',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            result = _json.loads(resp.read())
            app.logger.info(f'Resend送信成功: {to_email} id={result.get("id")}')
            return True
    except urllib.error.HTTPError as he:
        err_body = he.read().decode('utf-8', errors='replace')
        app.logger.error(f'Resend HTTPエラー {he.code}: {err_body}')
        return False
    except Exception as e:
        app.logger.error(f'Resend送信エラー: {type(e).__name__}: {e}')
        return False



@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """パスワードリセット要求"""
    message = None
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = AppUser.query.filter(
            db.func.lower(AppUser.email) == email, AppUser.is_active == True
        ).first()
        # セキュリティのため、ユーザーが存在しなくても同じメッセージを返す
        if user and user.email:
            token = secrets.token_urlsafe(32)
            expires = datetime.utcnow() + timedelta(hours=1)
            rt = PasswordResetToken(user_id=user.id, token=token, expires_at=expires)
            db.session.add(rt)
            db.session.commit()
            reset_url = url_for('reset_password', token=token, _external=True)
            # メール送信を別スレッドで実行（画面がフリーズしないように）
            import threading
            def _send_bg(email=user.email, url=reset_url):
                try:
                    result = _send_reset_email(email, url)
                    if result:
                        app.logger.info(f'パスワードリセットメール送信成功: {email}')
                    else:
                        app.logger.error(f'パスワードリセットメール送信失敗: {email}')
                except Exception as e:
                    app.logger.error(f'パスワードリセットメール例外: {email} - {e}')
            threading.Thread(target=_send_bg, daemon=True).start()
            sent = True  # 非同期なので常にTrue扱い
            message = "パスワードリセット用のメールを送信しました。"
        else:
            message = "入力されたメールアドレスに一致するアカウントが見つかりませんでした。"
    return render_template("forgot_password.html", message=message, error=error)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """パスワードリセット実行"""
    rt = PasswordResetToken.query.filter_by(token=token, used=False).first()
    error = None
    if not rt or rt.expires_at < datetime.utcnow():
        return render_template("reset_password.html", error="リセットリンクが無効または期限切れです。", token=token, expired=True)

    if request.method == "POST":
        new_password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_password) < 8:
            error = "パスワードは8文字以上にしてください。"
        elif new_password != confirm:
            error = "パスワードが一致しません。"
        else:
            user = AppUser.query.get(rt.user_id)
            user.password_hash = generate_password_hash(new_password)
            rt.used = True
            db.session.commit()
            return redirect(url_for('app_login') + '?reset=1')

    return render_template("reset_password.html", token=token, error=error, expired=False)


# ── ユーザー管理（オーナー専用） ──────────────────────────────

@app.route("/settings/users")
@login_required
def settings_users():
    """ユーザー管理ページ（super_admin: 管理者権限管理 / owner等: テナント内ユーザー管理）"""
    app_user = AppUser.query.get(session.get('app_user_id'))
    if not app_user or app_user.role not in ('owner', 'store_manager', 'super_admin', 'sys_admin'):
        return redirect(url_for('executive_dashboard'))
    if app_user.role in ('owner', 'store_manager'):
        return redirect(url_for('settings'))
    if app_user.role in ('super_admin', 'sys_admin'):
        # super_admin / sys_admin: クライアント管理画面の権限管理
        sys_admins = AppUser.query.filter(
            AppUser.role == 'sys_admin', AppUser.is_active == True
        ).order_by(AppUser.created_at).all()
        # super_admin 自身を先頭に追加してテンプレートに渡す
        all_admins = [app_user] + [u for u in sys_admins if u.id != app_user.id]
        return render_template("settings_admin_perms.html",
                               sys_admins=sys_admins,
                               all_admins=all_admins,
                               my_id=app_user.id,
                               is_super_admin=(app_user.role == 'super_admin'))
    stores      = get_allowed_stores(ignore_active=True)
    allowed_ids = [s.id for s in stores]
    users = AppUser.query.filter_by(is_active=True, tenant_id=app_user.tenant_id).order_by(AppUser.created_at).all()
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    return render_template("settings_users.html", stores=stores, users=users, staff_list=staff_list)


@app.route("/api/users", methods=["POST"])
@login_required
def api_user_create():
    """ユーザー作成（オーナーのみ）"""
    app_user = AppUser.query.get(session.get('app_user_id'))
    if not app_user or app_user.role not in ('owner', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    role     = data.get('role', 'staff')
    staff_id = data.get('staff_id') or None
    if not username or not password:
        return jsonify({'error': 'ユーザー名とパスワードは必須です'}), 400
    if AppUser.query.filter_by(username=username).first():
        return jsonify({'error': 'そのユーザー名は既に使用されています'}), 400
    u = AppUser(
        username=username, email=email or None,
        password_hash=generate_password_hash(password),
        role=role, staff_id=staff_id,
        tenant_id=app_user.tenant_id,  # 作成者と同じテナントに所属
        can_view_accounting=bool(data.get('can_view_accounting', True)),
        can_view_all_staff=bool(data.get('can_view_all_staff', True)),
        can_edit_kpi=bool(data.get('can_edit_kpi', True)),
        can_manage_uncollected=bool(data.get('can_manage_uncollected', True)),
        can_view_executive=bool(data.get('can_view_executive', True)),
        can_view_leads_page=bool(data.get('can_view_leads_page', True)),
        can_view_daily_report=bool(data.get('can_view_daily_report', True)),
        can_view_leave=bool(data.get('can_view_leave', True)),
    )
    db.session.add(u)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': u.id})


@app.route("/api/users/<int:uid>", methods=["PUT"])
@login_required
def api_user_update(uid):
    """ユーザー更新（オーナー・店長・super_admin）"""
    app_user = AppUser.query.get(session.get('app_user_id'))
    if not app_user or app_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    u = AppUser.query.get_or_404(uid)
    data = request.get_json() or {}
    if 'email'    in data: u.email    = (data['email'] or '').strip().lower() or None
    if 'role'     in data: u.role     = data['role']
    if 'staff_id' in data: u.staff_id = data['staff_id'] or None
    if 'password' in data and data['password']:
        u.password_hash = generate_password_hash(data['password'])
    if 'can_view_accounting'    in data: u.can_view_accounting    = bool(data['can_view_accounting'])
    if 'can_view_all_staff'     in data: u.can_view_all_staff     = bool(data['can_view_all_staff'])
    if 'can_edit_kpi'           in data: u.can_edit_kpi           = bool(data['can_edit_kpi'])
    if 'can_manage_uncollected' in data: u.can_manage_uncollected = bool(data['can_manage_uncollected'])
    if 'can_view_executive'     in data: u.can_view_executive     = bool(data['can_view_executive'])
    if 'can_view_leads_page'    in data: u.can_view_leads_page    = bool(data['can_view_leads_page'])
    if 'can_view_daily_report'  in data: u.can_view_daily_report  = bool(data['can_view_daily_report'])
    if 'can_view_leave'         in data: u.can_view_leave         = bool(data['can_view_leave'])
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@login_required
def api_user_delete(uid):
    """ユーザー無効化（オーナー・店長・super_admin）"""
    app_user = AppUser.query.get(session.get('app_user_id'))
    if not app_user or app_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    if uid == app_user.id:
        return jsonify({'error': '自分自身は削除できません'}), 400
    u = AppUser.query.get_or_404(uid)
    u.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/users")
@login_required
def api_user_list():
    """ユーザー一覧（オーナー：自テナントのみ / super_admin：全体）"""
    app_user = AppUser.query.get(session.get('app_user_id'))
    if not app_user or app_user.role not in ('owner', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    q = AppUser.query.filter_by(is_active=True)
    if app_user.role == 'owner':
        q = q.filter_by(tenant_id=app_user.tenant_id)
    users = q.order_by(AppUser.created_at).all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'email': u.email or '',
        'role': u.role,
        'staff_id': u.staff_id,
        'tenant_id': u.tenant_id,
        'can_view_accounting': u.can_view_accounting,
        'can_view_all_staff': u.can_view_all_staff,
        'can_edit_kpi': u.can_edit_kpi,
        'can_manage_uncollected': u.can_manage_uncollected,
        'last_login': u.last_login.isoformat() if u.last_login else None,
    } for u in users])


# ── テナント管理（super_admin専用） ────────────────────────

@app.route("/api/admin/store-data-check")
@super_admin_required
def api_admin_store_data_check():
    """全店舗のデータ件数を返す（デバッグ用）"""
    stores = Store.query.filter_by(is_active=True).all()
    result = []
    for s in stores:
        tenant = Tenant.query.get(s.tenant_id) if s.tenant_id else None
        kpi_count  = SalesKPI.query.filter_by(store_id=s.id).count()
        staff_count = Staff.query.filter_by(store_id=s.id, is_active=True).count()
        app_count  = ApplicationRecord.query.filter_by(store_id=s.id).count()
        result.append({
            'store_id': s.id,
            'store_name': s.name,
            'tenant_name': tenant.name if tenant else 'なし',
            'kpi_records': kpi_count,
            'staff_count': staff_count,
            'application_count': app_count,
        })
    return jsonify(result)


@app.route("/api/admin/clear-store-data/<int:sid>", methods=["POST"])
@super_admin_required
def api_admin_clear_store_data(sid):
    """指定店舗のデータを強制クリア"""
    store = Store.query.get_or_404(sid)
    kpi_del  = SalesKPI.query.filter_by(store_id=sid).delete()
    pl_del   = PLRecord.query.filter_by(store_id=sid).delete()
    app_del  = ApplicationRecord.query.filter_by(store_id=sid).delete()
    Staff.query.filter_by(store_id=sid).update({'is_active': False})
    db.session.commit()
    return jsonify({'status': 'ok', 'store_name': store.name,
                    'deleted_kpi': kpi_del, 'deleted_app': app_del})


@app.route("/admin/tenants")
@super_admin_required
def admin_tenants():
    """テナント管理ページ（super_adminのみ）"""
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    is_super_admin = cur_user and cur_user.role == 'super_admin'
    return render_template("admin_tenants.html", tenants=tenants, now=datetime.now(),
                           is_super_admin=is_super_admin)


@app.route("/admin/applications")
@super_admin_required
def admin_applications():
    """トライアル申込管理ページ（super_adminのみ）"""
    apps = TrialApplication.query.order_by(TrialApplication.created_at.desc()).all()
    total   = len(apps)
    new_cnt = sum(1 for a in apps if a.status == 'new')
    contacted_cnt  = sum(1 for a in apps if a.status == 'contacted')
    contracted_cnt = sum(1 for a in apps if a.status == 'contracted')
    rejected_cnt   = sum(1 for a in apps if a.status == 'rejected')
    return render_template("admin_applications.html", apps=apps,
                           total=total, new_cnt=new_cnt,
                           contacted_cnt=contacted_cnt,
                           contracted_cnt=contracted_cnt,
                           rejected_cnt=rejected_cnt)


@app.route("/api/admin/applications/<int:app_id>", methods=["DELETE"])
@super_admin_required
def api_admin_application_delete(app_id):
    """申込削除"""
    rec = TrialApplication.query.get_or_404(app_id)
    db.session.delete(rec)
    db.session.commit()
    return jsonify({'ok': True})


@app.route("/api/admin/applications/<int:app_id>", methods=["PATCH"])
@super_admin_required
def api_admin_application_update(app_id):
    """申込ステータス・メモ更新"""
    rec  = TrialApplication.query.get_or_404(app_id)
    data = request.get_json() or {}
    if 'status' in data:
        rec.status = data['status']
    if 'memo' in data:
        rec.memo = data['memo']
    db.session.commit()
    return jsonify({'ok': True})


@app.route("/api/tenants", methods=["GET"])
@super_admin_required
def api_tenants_get():
    """テナント一覧"""
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    result = []
    for t in tenants:
        stores = Store.query.filter_by(tenant_id=t.id, is_active=True).all()
        owner = AppUser.query.filter_by(tenant_id=t.id, role='owner', is_active=True).first()
        result.append({
            'id': t.id,
            'name': t.name,
            'plan': t.plan,
            'is_active': t.is_active,
            'subscription_status': t.subscription_status or 'trial',
            'trial_ends_at': t.trial_ends_at.strftime('%Y-%m-%dT%H:%M:%S') if t.trial_ends_at else None,
            'contract_start_date': t.contract_start_date.strftime('%Y-%m-%d') if t.contract_start_date else None,
            'store_count': len(stores),
            'owner_username': owner.username if owner else None,
            'owner_email': owner.email if owner else None,
            'created_at': t.created_at.strftime('%Y-%m-%d') if t.created_at else None,
        })
    return jsonify(result)


@app.route("/api/tenants", methods=["POST"])
@super_admin_required
def api_tenants_post():
    """テナント新規作成（デフォルト店舗+オーナーアカウントも作成）"""
    err = _check_admin_perm('admin_can_add_tenant')
    if err: return err
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '会社名は必須です'}), 400

    owner_username = (data.get('owner_username') or '').strip()
    owner_password = (data.get('owner_password') or '').strip()
    owner_email    = (data.get('owner_email') or '').strip()

    if not owner_username or not owner_password:
        return jsonify({'error': 'オーナーのユーザー名とパスワードは必須です'}), 400

    if AppUser.query.filter_by(username=owner_username).first():
        return jsonify({'error': 'そのユーザー名は既に使われています'}), 400

    trial_days = int(data.get('trial_days', 14))
    from datetime import timedelta
    trial_ends_at = datetime.utcnow() + timedelta(days=trial_days)

    tenant = Tenant(name=name, plan=data.get('plan', 'standard'), is_active=True,
                    trial_ends_at=trial_ends_at, subscription_status='trial')
    db.session.add(tenant)
    db.session.flush()

    store = Store(name=name, is_active=True, tenant_id=tenant.id)
    db.session.add(store)
    db.session.flush()

    owner = AppUser(
        username=owner_username,
        email=owner_email or None,
        password_hash=generate_password_hash(owner_password),
        role='owner',
        tenant_id=tenant.id,
        is_active=True,
    )
    db.session.add(owner)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': tenant.id})


@app.route("/api/tenants/<int:tid>", methods=["PUT"])
@super_admin_required
def api_tenant_update(tid):
    """テナント更新"""
    tenant = Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    if 'name' in data and data['name'].strip():
        tenant.name = data['name'].strip()
    if 'plan' in data:
        tenant.plan = data['plan']
    if 'is_active' in data:
        tenant.is_active = bool(data['is_active'])
    if 'contract_start_date' in data:
        from datetime import datetime as _dt
        try:
            tenant.contract_start_date = _dt.strptime(data['contract_start_date'], '%Y-%m-%d').date() if data['contract_start_date'] else None
        except Exception:
            pass
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/reset-all-data", methods=["POST"])
@super_admin_required
def api_tenant_reset_all_data(tid):
    """テナントの全業務データを削除（アカウント・店舗・テナントは残す）"""
    tenant = Tenant.query.get_or_404(tid)
    store_ids = [s.id for s in Store.query.filter_by(tenant_id=tid).all()]
    staff_ids  = [s.id for s in Staff.query.filter(Staff.store_id.in_(store_ids)).all()] if store_ids else []
    counts = {}

    if store_ids:
        counts['SalesKPI']             = SalesKPI.query.filter(SalesKPI.store_id.in_(store_ids)).delete(synchronize_session=False)
        counts['ApplicationRecord']    = ApplicationRecord.query.filter(ApplicationRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        counts['EchoRecord']           = EchoRecord.query.filter(EchoRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        counts['CustomerServiceRecord']= CustomerServiceRecord.query.filter(CustomerServiceRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        counts['PLRecord']             = PLRecord.query.filter(PLRecord.store_id.in_(store_ids)).delete(synchronize_session=False)

        # 日報関連
        report_ids = [r.id for r in DailyReport.query.filter(DailyReport.store_id.in_(store_ids)).all()]
        if report_ids:
            DailyReportCustomer.query.filter(DailyReportCustomer.report_id.in_(report_ids)).delete(synchronize_session=False)
            DailyTaskCheck.query.filter(DailyTaskCheck.report_id.in_(report_ids)).delete(synchronize_session=False)
            counts['DailyReport'] = DailyReport.query.filter(DailyReport.store_id.in_(store_ids)).delete(synchronize_session=False)

    if staff_ids:
        counts['LeaveRecord'] = LeaveRecord.query.filter(LeaveRecord.staff_id.in_(staff_ids)).delete(synchronize_session=False)
        try:
            counts['LeaveAllocation'] = LeaveAllocation.query.filter(LeaveAllocation.staff_id.in_(staff_ids)).delete(synchronize_session=False)
        except Exception:
            pass

    # UncollectedPayment
    if store_ids:
        try:
            counts['UncollectedPayment'] = UncollectedPayment.query.filter(UncollectedPayment.store_id.in_(store_ids)).delete(synchronize_session=False)
        except Exception:
            pass

    db.session.commit()
    return jsonify({'status': 'ok', 'tenant_name': tenant.name, 'deleted': counts})


@app.route("/api/tenants/<int:tid>/activate", methods=["POST"])
@super_admin_required
def api_tenant_activate(tid):
    """トライアル → 契約開始（super_admin / sys_admin 共通可）"""
    from datetime import date as _date
    tenant = Tenant.query.get_or_404(tid)
    tenant.subscription_status = 'active'
    tenant.is_active = True
    if not tenant.contract_start_date:
        tenant.contract_start_date = _date.today()
    db.session.commit()
    return jsonify({'status': 'ok', 'contract_start_date': tenant.contract_start_date.strftime('%Y-%m-%d')})


@app.route("/api/admin-users", methods=["GET"])
@super_admin_required
def api_admin_users_list():
    """sys_admin ユーザー一覧（super_admin のみ全表示）"""
    users = AppUser.query.filter(
        AppUser.role.in_(['sys_admin']),
        AppUser.is_active == True
    ).all()
    return jsonify([{
        'id': u.id, 'username': u.username,
        'email': u.email or '',
        'created_at': u.created_at.strftime('%Y-%m-%d') if u.created_at else None,
        'admin_can_add_tenant':    bool(getattr(u, 'admin_can_add_tenant',    False)),
        'admin_can_manage_stores': bool(getattr(u, 'admin_can_manage_stores', False)),
        'admin_can_delete_tenant': bool(getattr(u, 'admin_can_delete_tenant', False)),
        'admin_can_lock_tenant':   bool(getattr(u, 'admin_can_lock_tenant',   False)),
    } for u in users])


@app.route("/api/admin-users", methods=["POST"])
@super_admin_required
def api_admin_users_create():
    """sys_admin ユーザー作成（super_admin のみ）"""
    if session.get('app_user_role') != 'super_admin':
        return jsonify({'error': 'スーパー管理者のみ作成できます'}), 403
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    email    = (data.get('email') or '').strip()
    if not username or not password:
        return jsonify({'error': 'ユーザー名とパスワードは必須です'}), 400
    if AppUser.query.filter_by(username=username).first():
        return jsonify({'error': 'そのユーザー名は既に使われています'}), 400
    user = AppUser(
        username=username,
        email=email or None,
        password_hash=generate_password_hash(password),
        role='sys_admin',
        tenant_id=None,
        is_active=True,
        admin_can_add_tenant=bool(data.get('admin_can_add_tenant', False)),
        admin_can_manage_stores=bool(data.get('admin_can_manage_stores', False)),
        admin_can_delete_tenant=bool(data.get('admin_can_delete_tenant', False)),
        admin_can_lock_tenant=bool(data.get('admin_can_lock_tenant', False)),
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': user.id})


@app.route("/api/admin-users/<int:uid>", methods=["PUT"])
@super_admin_required
def api_admin_users_update(uid):
    """sys_admin の権限更新（super_admin のみ）"""
    if session.get('app_user_role') != 'super_admin':
        return jsonify({'error': 'スーパー管理者のみ変更できます'}), 403
    user = AppUser.query.get_or_404(uid)
    data = request.get_json() or {}
    for field in ['admin_can_add_tenant','admin_can_manage_stores','admin_can_delete_tenant','admin_can_lock_tenant']:
        if field in data:
            setattr(user, field, bool(data[field]))
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/my-account", methods=["PUT"])
@login_required
def api_my_account_update():
    """ログイン中のアカウント情報を更新（メール・パスワード）"""
    user = AppUser.query.get(session.get('app_user_id'))
    if not user:
        return jsonify({'error': '認証エラー'}), 401
    data = request.get_json() or {}
    if 'email' in data and data['email']:
        user.email = data['email'].strip().lower()
    if 'password' in data and data['password']:
        if len(data['password']) < 8:
            return jsonify({'error': 'パスワードは8文字以上にしてください'}), 400
        user.password_hash = generate_password_hash(data['password'])
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/admin-users/<int:uid>", methods=["DELETE"])
@super_admin_required
def api_admin_users_delete(uid):
    """sys_admin ユーザー削除（super_admin のみ）"""
    if session.get('app_user_role') != 'super_admin':
        return jsonify({'error': 'スーパー管理者のみ削除できます'}), 403
    user = AppUser.query.get_or_404(uid)
    if user.role not in ('sys_admin',):
        return jsonify({'error': '削除できないアカウントです'}), 400
    user.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/lock", methods=["POST"])
@super_admin_only
def api_tenant_lock(tid):
    """テナントをロック（アクセス不可）"""
    err = _check_admin_perm("admin_can_lock_tenant")
    if err: return err
    tenant = Tenant.query.get_or_404(tid)
    tenant.subscription_status = 'locked'
    tenant.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/unlock", methods=["POST"])
@super_admin_only
def api_tenant_unlock(tid):
    """テナントを有効化（ロック解除）"""
    err = _check_admin_perm("admin_can_lock_tenant")
    if err: return err
    from datetime import date as _date
    tenant = Tenant.query.get_or_404(tid)
    tenant.subscription_status = 'active'
    tenant.is_active = True
    # 契約開始日が未設定なら今日をセット
    if not tenant.contract_start_date:
        tenant.contract_start_date = _date.today()
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/extend-trial", methods=["POST"])
@super_admin_only
def api_tenant_extend_trial(tid):
    """トライアル期間を延長"""
    from datetime import timedelta
    tenant = Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    days = int(data.get('days', 7))
    base = tenant.trial_ends_at if (tenant.trial_ends_at and tenant.trial_ends_at > datetime.utcnow()) else datetime.utcnow()
    tenant.trial_ends_at = base + timedelta(days=days)
    tenant.subscription_status = 'trial'
    tenant.is_active = True
    db.session.commit()
    return jsonify({'status': 'ok', 'trial_ends_at': tenant.trial_ends_at.strftime('%Y-%m-%d')})


@app.route("/api/admin/test-email", methods=["POST"])
@super_admin_required
def api_test_email():
    """メール送信テスト（super_adminのみ）"""
    data = request.get_json() or {}
    to_email = data.get("email", "").strip()
    if not to_email:
        return jsonify({"error": "emailが必要です"}), 400
    try:
        # Resendを直接呼んでエラー詳細を取得
        import urllib.request, urllib.error, json as _json
        resend_key = os.getenv('RESEND_API_KEY', '')
        if not resend_key:
            return jsonify({"status": "error", "message": "RESEND_API_KEY が Railway に未設定"}), 500
        from_addr = os.getenv('MAIL_FROM', 'onboarding@resend.dev')
        payload = _json.dumps({
            'from': f'ミエルーム <{from_addr}>',
            'to': [to_email],
            'subject': '【ミエルーム】メール送信テスト',
            'html': '<p>ミエルームのテストメールです。</p>'
        }).encode('utf-8')
        import ssl
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            'https://api.resend.com/emails',
            data=payload,
            headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'mieroom-app/1.0',
                'Accept': 'application/json',
            },
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                result = _json.loads(resp.read())
                return jsonify({"status": "ok", "message": f"{to_email} に送信成功", "id": result.get("id")})
        except urllib.error.HTTPError as he:
            err_body = he.read().decode('utf-8', errors='replace')
            return jsonify({"status": "error", "http_status": he.code, "message": err_body[:500]}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/law")
def law_page():
    """特定商取引法に基づく表示ページ"""
    return render_template("law.html")


@app.route("/apply", methods=["GET", "POST"])
def apply_page():
    """無料トライアル申し込みフォーム"""
    success = False
    error   = None
    form    = {}

    if request.method == "POST":
        company = request.form.get("company", "").strip()
        name    = request.form.get("name", "").strip()
        email   = request.form.get("email", "").strip()
        phone   = request.form.get("phone", "").strip()
        stores  = request.form.get("stores", "").strip()
        message = request.form.get("message", "").strip()
        form    = {"company": company, "name": name, "email": email,
                   "phone": phone, "stores": stores, "message": message}

        if not all([company, name, email, phone, stores]):
            error = "必須項目をすべて入力してください。"
        else:
            # DBに保存
            try:
                app_record = TrialApplication(
                    company=company, name=name, email=email,
                    phone=phone, stores=stores, message=message
                )
                db.session.add(app_record)
                db.session.commit()
            except Exception as e:
                app.logger.error(f'TrialApplication DB保存エラー: {e}')
                db.session.rollback()

            # 管理者への通知メール
            import threading
            def _notify():
                try:
                    import urllib.request, json as _json, ssl
                    resend_key = os.getenv('RESEND_API_KEY', '')
                    if not resend_key:
                        return
                    body_html = f"""
<h2>【ミエルーム】新規トライアル申し込み</h2>
<table border="1" cellpadding="8" style="border-collapse:collapse;">
  <tr><th>会社名</th><td>{company}</td></tr>
  <tr><th>担当者名</th><td>{name}</td></tr>
  <tr><th>メール</th><td>{email}</td></tr>
  <tr><th>電話番号</th><td>{phone}</td></tr>
  <tr><th>店舗数</th><td>{stores}</td></tr>
  <tr><th>メッセージ</th><td>{message or "なし"}</td></tr>
</table>"""
                    payload = _json.dumps({
                        'from': 'ミエルーム申込通知 <onboarding@resend.dev>',
                        'to':   ['mieroom.cloud@gmail.com'],
                        'reply_to': email,
                        'subject': f'【申込】{company} - ミエルーム トライアル申請',
                        'html': body_html,
                    }).encode('utf-8')
                    req = urllib.request.Request(
                        'https://api.resend.com/emails', data=payload,
                        headers={'Authorization': f'Bearer {resend_key}',
                                 'Content-Type': 'application/json',
                                 'User-Agent': 'mieroom-app/1.0'},
                        method='POST')
                    ctx = ssl.create_default_context()
                    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                        app.logger.info(f'管理者通知メール送信成功: {company} → mieroom.cloud@gmail.com')

                    # サンクスメール（申込者へ）
                    thanks_html = f"""
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;">
  <div style="text-align:center;margin-bottom:24px;">
    <h2 style="color:#0D9488;margin:0;">ミエルーム</h2>
    <p style="color:#6b7280;font-size:13px;margin:4px 0 0;">不動産賃貸仲介業務管理システム</p>
  </div>
  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:32px;">
    <h3 style="color:#111827;margin:0 0 16px;">お申し込みありがとうございます</h3>
    <p style="color:#374151;line-height:1.8;">{name} 様<br><br>
    この度は、ミエルームの無料トライアルにお申し込みいただきありがとうございます。<br><br>
    担当者より <strong>1〜2営業日以内</strong> にご連絡いたします。<br>
    今しばらくお待ちください。</p>
    <div style="background:#F0FDFA;border-radius:8px;padding:16px;margin-top:20px;">
      <p style="margin:0;font-size:13px;color:#0F766E;font-weight:600;">お申し込み内容</p>
      <table style="margin-top:8px;font-size:13px;color:#374151;width:100%;">
        <tr><td style="padding:3px 0;color:#6b7280;">会社名</td><td>{company}</td></tr>
        <tr><td style="padding:3px 0;color:#6b7280;">担当者名</td><td>{name}</td></tr>
        <tr><td style="padding:3px 0;color:#6b7280;">電話番号</td><td>{phone}</td></tr>
        <tr><td style="padding:3px 0;color:#6b7280;">店舗数</td><td>{stores}</td></tr>
      </table>
    </div>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
    <p style="color:#9ca3af;font-size:12px;margin:0;">
      ご不明な点は <a href="mailto:mieroom.cloud@gmail.com" style="color:#0D9488;">mieroom.cloud@gmail.com</a> までご連絡ください。
    </p>
  </div>
</div>"""
                    thanks_payload = _json.dumps({
                        'from': 'ミエルーム <onboarding@resend.dev>',
                        'to': [email],
                        'subject': '【ミエルーム】お申し込みありがとうございます',
                        'html': thanks_html,
                    }).encode('utf-8')
                    thanks_req = urllib.request.Request(
                        'https://api.resend.com/emails', data=thanks_payload,
                        headers={'Authorization': f'Bearer {resend_key}',
                                 'Content-Type': 'application/json',
                                 'User-Agent': 'mieroom-app/1.0'},
                        method='POST')
                    with urllib.request.urlopen(thanks_req, timeout=15, context=ctx) as r:
                        app.logger.info(f'サンクスメール送信成功: {email}')

                except Exception as e:
                    app.logger.error(f'申込通知メールエラー: {e}')
            threading.Thread(target=_notify, daemon=True).start()
            success = True

    return render_template("apply.html", success=success, error=error,
                           form=form, email=form.get("email",""))


@app.route("/terms")
def terms_page():
    """利用規約ページ"""
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    """プライバシーポリシーページ"""
    return render_template("privacy.html")


@app.route("/trial-expired")
def trial_expired_page():
    """トライアル期限切れ・ロック時のページ"""
    return render_template("trial_expired.html")


@app.route("/api/tenants/<int:tid>", methods=["DELETE"])
@super_admin_only
def api_tenant_delete(tid):
    """テナント物理削除（関連データを全てカスケード削除）"""
    err = _check_admin_perm("admin_can_delete_tenant")
    if err: return err
    tenant = Tenant.query.get_or_404(tid)

    # 対象の店舗・スタッフIDを収集
    store_ids = [s.id for s in Store.query.filter_by(tenant_id=tid).all()]

    if store_ids:
        # 日報の子テーブルを先に削除
        report_ids = [r.id for r in DailyReport.query.filter(
            DailyReport.store_id.in_(store_ids)).all()]
        if report_ids:
            DailyTaskCheck.query.filter(
                DailyTaskCheck.report_id.in_(report_ids)).delete(synchronize_session=False)
            DailyReportCustomer.query.filter(
                DailyReportCustomer.report_id.in_(report_ids)).delete(synchronize_session=False)
            DailyReport.query.filter(
                DailyReport.id.in_(report_ids)).delete(synchronize_session=False)

        DailyTaskTemplate.query.filter(
            DailyTaskTemplate.store_id.in_(store_ids)).delete(synchronize_session=False)
        LeaveBalance.query.filter(
            LeaveBalance.store_id.in_(store_ids)).delete(synchronize_session=False)
        LeaveRecord.query.filter(
            LeaveRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        SalesKPI.query.filter(
            SalesKPI.store_id.in_(store_ids)).delete(synchronize_session=False)
        UncollectedPayment.query.filter(
            UncollectedPayment.store_id.in_(store_ids)).delete(synchronize_session=False)
        Lead.query.filter(
            Lead.store_id.in_(store_ids)).delete(synchronize_session=False)
        ApplicationRecord.query.filter(
            ApplicationRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        ContractRecord.query.filter(
            ContractRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        LeadMediaStat.query.filter(
            LeadMediaStat.store_id.in_(store_ids)).delete(synchronize_session=False)
        AdCost.query.filter(
            AdCost.store_id.in_(store_ids)).delete(synchronize_session=False)
        PLCustomValue.query.filter(
            PLCustomValue.store_id.in_(store_ids)).delete(synchronize_session=False)
        PLCustomItem.query.filter(
            PLCustomItem.store_id.in_(store_ids)).delete(synchronize_session=False)
        PLRecord.query.filter(
            PLRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        MediaType.query.filter(
            MediaType.store_id.in_(store_ids)).delete(synchronize_session=False)
        StatusColor.query.filter(
            StatusColor.store_id.in_(store_ids)).delete(synchronize_session=False)
        EchoRecord.query.filter(
            EchoRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        CustomerServiceRecord.query.filter(
            CustomerServiceRecord.store_id.in_(store_ids)).delete(synchronize_session=False)
        Staff.query.filter(
            Staff.store_id.in_(store_ids)).delete(synchronize_session=False)
        Store.query.filter(
            Store.id.in_(store_ids)).delete(synchronize_session=False)

    # AppUser とそのリセットトークンを削除
    user_ids = [u.id for u in AppUser.query.filter_by(tenant_id=tid).all()]
    if user_ids:
        PasswordResetToken.query.filter(
            PasswordResetToken.user_id.in_(user_ids)).delete(synchronize_session=False)
        AppUser.query.filter(
            AppUser.id.in_(user_ids)).delete(synchronize_session=False)

    db.session.delete(tenant)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/stores", methods=["GET"])
@super_admin_required
def api_tenant_stores(tid):
    """テナント内の店舗一覧"""
    stores = Store.query.filter_by(tenant_id=tid, is_active=True).order_by(Store.created_at.asc()).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'is_locked': bool(getattr(s, 'is_locked', False)),
        'contract_start_date': s.contract_start_date.strftime('%Y-%m-%d') if getattr(s, 'contract_start_date', None) else None,
        'created_at': s.created_at.strftime('%Y-%m-%d') if s.created_at else None
    } for s in stores])


@app.route("/api/tenants/<int:tid>/stores", methods=["POST"])
@super_admin_only
def api_tenant_store_add(tid):
    """テナントに店舗を追加"""
    err = _check_admin_perm("admin_can_manage_stores")
    if err: return err
    Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '店舗名は必須です'}), 400
    store = Store(name=name, is_active=True, tenant_id=tid)
    db.session.add(store)
    db.session.flush()  # IDを確定させる
    # 新店舗IDに万が一紐づく既存データを全削除（まっさら保証）
    sid = store.id
    SalesKPI.query.filter_by(store_id=sid).delete()
    PLRecord.query.filter_by(store_id=sid).delete()
    ApplicationRecord.query.filter_by(store_id=sid).delete()
    Staff.query.filter_by(store_id=sid).delete()
    UncollectedPayment.query.filter_by(store_id=sid).delete()
    try:
        EchoRecord.query.filter_by(store_id=sid).delete()
        CustomerServiceRecord.query.filter_by(store_id=sid).delete()
    except Exception: pass
    db.session.commit()
    return jsonify({'status': 'ok', 'id': store.id})


@app.route("/api/tenants/<int:tid>/stores/<int:sid>", methods=["PUT"])
@super_admin_required
def api_tenant_store_update(tid, sid):
    """店舗情報を更新（名前・ロック・契約開始日）"""
    store = Store.query.filter_by(id=sid, tenant_id=tid).first_or_404()
    data = request.get_json() or {}
    if 'name' in data:
        name = (data['name'] or '').strip()
        if name: store.name = name
    if 'is_locked' in data:
        store.is_locked = bool(data['is_locked'])
        store.is_active = not bool(data['is_locked'])
    if 'contract_start_date' in data:
        from datetime import datetime as _dt
        try:
            store.contract_start_date = _dt.strptime(data['contract_start_date'], '%Y-%m-%d').date() if data['contract_start_date'] else None
        except Exception:
            pass
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/owner", methods=["PUT"])
@super_admin_required
def api_tenant_owner_update(tid):
    """テナントのオーナーアカウント情報を更新（ユーザー名/パスワード/メール）"""
    owner = AppUser.query.filter_by(tenant_id=tid, role='owner', is_active=True).first()
    if not owner:
        return jsonify({'error': 'オーナーアカウントが見つかりません'}), 404
    data = request.get_json() or {}
    if 'username' in data and data['username'].strip():
        new_name = data['username'].strip()
        if AppUser.query.filter(AppUser.username == new_name, AppUser.id != owner.id).first():
            return jsonify({'error': 'そのユーザー名は既に使われています'}), 400
        owner.username = new_name
    if 'email' in data:
        owner.email = (data['email'] or '').strip() or None
    if 'password' in data and data['password']:
        if len(data['password']) < 8:
            return jsonify({'error': 'パスワードは8文字以上にしてください'}), 400
        owner.password_hash = generate_password_hash(data['password'])
    db.session.commit()
    return jsonify({'status': 'ok', 'username': owner.username, 'email': owner.email or ''})


@app.route("/api/tenants/<int:tid>/stores/<int:sid>/reset-data", methods=["POST"])
@super_admin_required
def api_tenant_store_reset_data(tid, sid):
    """店舗の全データをリセット（完全にまっさらな状態に戻す）"""
    store = Store.query.filter_by(id=sid, tenant_id=tid).first_or_404()
    # 1. KPI・売上データ
    SalesKPI.query.filter_by(store_id=sid).delete()
    PLRecord.query.filter_by(store_id=sid).delete()
    # 2. 申込データ
    ApplicationRecord.query.filter_by(store_id=sid).delete()
    # 3. 反響・接客データ
    try:
        EchoRecord.query.filter_by(store_id=sid).delete()
    except Exception: pass
    try:
        CustomerServiceRecord.query.filter_by(store_id=sid).delete()
    except Exception: pass
    # 4. スタッフと有給
    staff_ids = [s.id for s in Staff.query.filter_by(store_id=sid).all()]
    if staff_ids:
        LeaveRecord.query.filter(LeaveRecord.staff_id.in_(staff_ids)).delete(synchronize_session=False)
        LeaveBalance.query.filter(LeaveBalance.staff_id.in_(staff_ids)).delete(synchronize_session=False)
    Staff.query.filter_by(store_id=sid).delete()
    # 5. 未入金データ
    UncollectedPayment.query.filter_by(store_id=sid).delete()
    db.session.commit()
    return jsonify({'status': 'ok', 'store_name': store.name})


@app.route("/api/tenants/<int:tid>/stores/<int:sid>", methods=["DELETE"])
@super_admin_only
def api_tenant_store_delete(tid, sid):
    """店舗を論理削除"""
    err = _check_admin_perm("admin_can_manage_stores")
    if err: return err
    store = Store.query.filter_by(id=sid, tenant_id=tid).first_or_404()
    store.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/staff", methods=["POST"])
@super_admin_required
def api_tenant_staff_add(tid):
    """テナントにスタッフを追加"""
    Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    store_id = int(data.get('store_id') or 0) or None
    role = (data.get('role') or '営業').strip()
    if not name:
        return jsonify({'error': 'スタッフ名は必須です'}), 400
    if store_id:
        store = Store.query.filter_by(id=store_id, tenant_id=tid, is_active=True).first()
        if not store:
            return jsonify({'error': '指定した店舗が見つかりません'}), 400
    staff = Staff(name=name, store_id=store_id, role=role, is_active=True)
    db.session.add(staff)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': staff.id, 'name': staff.name})


# ── テナントのログインアカウント管理 (super_admin 専用) ─────────────────────

@app.route("/api/tenants/<int:tid>/users", methods=["GET"])
@super_admin_required
def api_tenant_users_list(tid):
    Tenant.query.get_or_404(tid)
    users = AppUser.query.filter_by(tenant_id=tid, is_active=True).order_by(AppUser.created_at).all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'email': u.email or '',
        'role': u.role,
        'last_login': u.last_login.strftime('%Y-%m-%d %H:%M') if u.last_login else None,
    } for u in users])


@app.route("/api/tenants/<int:tid>/users", methods=["POST"])
@super_admin_required
def api_tenant_users_add(tid):
    Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    role = (data.get('role') or 'staff').strip()
    email = (data.get('email') or '').strip() or None
    if not username or not password:
        return jsonify({'error': 'ユーザー名とパスワードは必須です'}), 400
    if AppUser.query.filter_by(username=username).first():
        return jsonify({'error': 'そのユーザー名は既に使用されています'}), 400
    user = AppUser(
        tenant_id=tid,
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        email=email,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': user.id, 'username': user.username, 'role': user.role})


@app.route("/api/tenants/<int:tid>/users/<int:uid>/password", methods=["PUT"])
@super_admin_required
def api_tenant_user_password(tid, uid):
    user = AppUser.query.filter_by(id=uid, tenant_id=tid).first_or_404()
    data = request.get_json() or {}
    password = (data.get('password') or '').strip()
    if not password:
        return jsonify({'error': 'パスワードを入力してください'}), 400
    user.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/tenants/<int:tid>/users/<int:uid>", methods=["DELETE"])
@super_admin_required
def api_tenant_user_delete(tid, uid):
    user = AppUser.query.filter_by(id=uid, tenant_id=tid).first_or_404()
    if user.role == 'owner':
        return jsonify({'error': 'オーナーアカウントは削除できません'}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/me")
@login_required
def api_me():
    """ログインユーザーの診断情報を返す（テナント・店舗・権限）"""
    uid = session.get('app_user_id')
    user = AppUser.query.get(uid)
    if not user:
        return jsonify({'error': 'user not found'}), 404
    allowed_ids = get_allowed_store_ids()
    allowed_ids_ignore = get_allowed_store_ids(ignore_active=True)
    stores_info = []
    for sid in allowed_ids_ignore:
        s = Store.query.get(sid)
        stores_info.append({'id': sid, 'name': s.name if s else '?', 'is_active_selected': sid in allowed_ids})
    tenant = Tenant.query.get(user.tenant_id) if user.tenant_id else None
    return jsonify({
        'user_id':    user.id,
        'username':   user.username,
        'role':       user.role,
        'tenant_id':  user.tenant_id,
        'tenant_name': tenant.name if tenant else None,
        'tenant_plan': tenant.plan if tenant else None,
        'store_id':   user.store_id,
        'active_store_id': session.get('active_store_id'),
        'allowed_store_ids': allowed_ids,
        'all_store_ids': allowed_ids_ignore,
        'stores': stores_info,
        'STORE_ID_template': allowed_ids[0] if allowed_ids else None,
    })


@app.route("/api/admin/data-audit")
@super_admin_required
def api_admin_data_audit():
    """テナント別データ件数を確認（テナント分離診断ツール）"""
    result = {}
    stores = Store.query.all()
    for s in stores:
        tenant = Tenant.query.get(s.tenant_id) if s.tenant_id else None
        result[s.id] = {
            'store_name':  s.name,
            'tenant_name': tenant.name if tenant else 'なし',
            'pl_records':     PLRecord.query.filter_by(store_id=s.id).count(),
            'lead_stats':     LeadMediaStat.query.filter_by(store_id=s.id).count(),
            'leads':          Lead.query.filter_by(store_id=s.id).count(),
            'kpis':           SalesKPI.query.filter_by(store_id=s.id).count(),
            'staff':          Staff.query.filter_by(store_id=s.id, is_active=True).count(),
        }
    return jsonify(result)


# ── 申込一覧 API ──────────────────────────────────────────

def _parse_date(s):
    if not s: return None
    try: return datetime.strptime(s, '%Y-%m-%d').date()
    except: return None


def _app_record_to_dict(r, staff_map):
    return {
        'id': r.id,
        'staff_id': r.staff_id,
        'staff_name': staff_map.get(r.staff_id, '-') if r.staff_id else '-',
        'application_date': r.application_date.isoformat() if r.application_date else None,
        'media': r.media or '',
        'property_name': r.property_name or '',
        'room_number': r.room_number or '',
        'customer_name': r.customer_name or '',
        'rent': r.rent or 0,
        'management_company': r.management_company or '',
        'review_ng': bool(r.review_ng),
        'review_status': r.review_status or None,
        'contract_start_date': r.contract_start_date.isoformat() if r.contract_start_date else None,
        'ad_payment_date': r.ad_payment_date.isoformat() if r.ad_payment_date else None,
        'brokerage_fee': r.brokerage_fee or 0,
        'option_amount': r.option_amount or 0,
        'ad_type': r.ad_type or 'amount',
        'ad_amount': r.ad_amount or 0,
        'ad_amount_yen': round((r.rent or 0) * (r.ad_amount or 0) / 100) if (r.ad_type or 'amount') == 'percent' else (r.ad_amount or 0),
        'lifeline': bool(r.lifeline),
        'moving': bool(r.moving),
        'fire_insurance': bool(r.fire_insurance),
        'status': r.status or '申込',
        'ad_settled': bool(r.ad_settled),
        'ad_approved': bool(r.ad_approved),
        'brokerage_payment_date': r.brokerage_payment_date.strftime('%Y-%m-%d') if r.brokerage_payment_date else None,
        'brokerage_settled': bool(r.brokerage_settled),
        'brokerage_approved': bool(r.brokerage_approved),
        'option_settled': bool(r.option_settled),
        'option_approved': bool(r.option_approved),
        'option_payment_date': r.option_payment_date.strftime('%Y-%m-%d') if r.option_payment_date else None,
        'created_at': r.created_at.isoformat() if r.created_at else None,
    }


def _approved_in_month(rec, field, year, month):
    """指定方向(field)が、指定年月に入金（承認）済みかを返す。
    入金日(payment_date)の年月で判定する。"""
    if field == 'brokerage':
        ok, dt, amt = rec.brokerage_approved, rec.brokerage_payment_date, (rec.brokerage_fee or 0)
    elif field == 'option':
        ok, dt, amt = rec.option_approved, rec.option_payment_date, (rec.option_amount or 0)
    elif field == 'ad':
        ok, dt, amt = rec.ad_approved, rec.ad_payment_date, _ad_yen(rec)
    else:
        return False
    return bool(ok and amt != 0 and dt and dt.year == year and dt.month == month)


@app.route("/api/applications/settled")
@login_required
def api_applications_settled():
    """入金済み一覧：その月に入金（承認）された方向がある案件を返す。
    入金日(payment_date)の月で判定（6月申込/7月AD入金→7月の一覧に表示）。"""
    allowed_ids = get_allowed_store_ids()
    store_id = request.args.get('store_id', type=int)
    staff_id = request.args.get('staff_id', type=int)
    year  = request.args.get('year',  type=int)
    month = request.args.get('month', type=int)

    q = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
    )
    if store_id and store_id in allowed_ids:
        q = q.filter(ApplicationRecord.store_id == store_id)
    if staff_id:
        q = q.filter(ApplicationRecord.staff_id == staff_id)

    if year and month:
        # その月に入金日がある方向を持つ案件のみ
        q = q.filter(db.or_(
            db.and_(ApplicationRecord.brokerage_approved == True, ApplicationRecord.brokerage_fee != 0,
                    db.extract('year', ApplicationRecord.brokerage_payment_date) == year,
                    db.extract('month', ApplicationRecord.brokerage_payment_date) == month),
            db.and_(ApplicationRecord.option_approved == True, ApplicationRecord.option_amount != 0,
                    db.extract('year', ApplicationRecord.option_payment_date) == year,
                    db.extract('month', ApplicationRecord.option_payment_date) == month),
            db.and_(ApplicationRecord.ad_approved == True, ApplicationRecord.ad_amount != 0,
                    db.extract('year', ApplicationRecord.ad_payment_date) == year,
                    db.extract('month', ApplicationRecord.ad_payment_date) == month),
        ))
    else:
        q = q.filter(db.or_(
            db.and_(ApplicationRecord.brokerage_fee != 0, ApplicationRecord.brokerage_approved == True),
            db.and_(ApplicationRecord.ad_amount != 0, ApplicationRecord.ad_approved == True),
            db.and_(ApplicationRecord.option_amount != 0, ApplicationRecord.option_approved == True),
        ))

    recs = q.order_by(ApplicationRecord.application_date.asc(), ApplicationRecord.id.asc()).all()
    staff_map = {s.id: s.name for s in Staff.query.filter(Staff.id.in_({r.staff_id for r in recs if r.staff_id})).all()}
    # フロントが当月入金分を判定できるよう、対象年月も返す
    out = []
    for r in recs:
        d = _app_record_to_dict(r, staff_map)
        if year and month:
            d['brokerage_paid_this_month'] = _approved_in_month(r, 'brokerage', year, month)
            d['option_paid_this_month']    = _approved_in_month(r, 'option', year, month)
            d['ad_paid_this_month']        = _approved_in_month(r, 'ad', year, month)
        out.append(d)
    return jsonify(out)


@app.route("/api/applications/unpaid")
@login_required
def api_applications_unpaid():
    """未入金一覧：仲介またはADが未承認の申込レコードを返す（月フィルタ）"""
    allowed_ids = get_allowed_store_ids()
    store_id = request.args.get('store_id', type=int)
    staff_id = request.args.get('staff_id', type=int)
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]

    from datetime import date as _date
    # その月以前に申し込まれた未承認レコードを表示（入力した月から記載）
    month_end = _date(year, month, 28)  # その月末日以前
    try:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        month_end = _date(year, month, last_day)
    except Exception:
        pass

    q = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
        ApplicationRecord.application_date <= month_end,  # 選択月以前の案件のみ
        db.or_(
            db.and_(ApplicationRecord.brokerage_fee != 0, ApplicationRecord.brokerage_approved == False),
            db.and_(ApplicationRecord.ad_amount != 0,     ApplicationRecord.ad_approved == False),
            db.and_(ApplicationRecord.option_amount != 0, ApplicationRecord.option_approved == False),
        )
    )
    if store_id and store_id in allowed_ids:
        q = q.filter(ApplicationRecord.store_id == store_id)
    if staff_id:
        q = q.filter(ApplicationRecord.staff_id == staff_id)

    # 古いものが上（申込日昇順）
    recs = q.order_by(ApplicationRecord.application_date.asc(), ApplicationRecord.id.asc()).all()
    staff_ids = list({r.staff_id for r in recs if r.staff_id})
    staff_map = {s.id: s.name for s in Staff.query.filter(Staff.id.in_(staff_ids)).all()} if staff_ids else {}
    return jsonify([_app_record_to_dict(r, staff_map) for r in recs])


@app.route("/api/applications/approved-sum")
@login_required
def api_applications_approved_sum():
    """入金済み（全承認済み）申込の売上合計を返す（年月フィルタ付き）"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    allowed = get_allowed_store_ids()

    recs = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed),
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
        db.extract('year',  ApplicationRecord.application_date) == year,
        db.extract('month', ApplicationRecord.application_date) == month,
    ).all()

    # 仲手・その他費用・AD の承認済み金額をそれぞれ独立に合算（その他費用も売上に含む）
    total = sum(_record_approved_amount(r) for r in recs)
    count = sum(1 for r in recs if _record_approved_amount(r) > 0)
    return jsonify({'total': total, 'count': count})


def _ad_yen(r):
    if (r.ad_type or 'amount') == 'percent':
        return round((r.rent or 0) * (r.ad_amount or 0) / 100)
    return r.ad_amount or 0


def _record_total_amount(r):
    """案件の満額（仲手＋その他費用＋AD）"""
    return (r.brokerage_fee or 0) + (r.option_amount or 0) + _ad_yen(r)


def _record_approved_amount(r):
    """入金済み（承認済み）金額（方向ごとに独立加算）"""
    amt = 0
    if (r.brokerage_fee or 0) != 0 and r.brokerage_approved:
        amt += r.brokerage_fee or 0
    if (r.option_amount or 0) != 0 and r.option_approved:
        amt += r.option_amount or 0
    ady = _ad_yen(r)
    if ady != 0 and r.ad_approved:
        amt += ady
    return amt


@app.route("/api/applications/summary")
@login_required
def api_applications_summary():
    """顧客管理表の集計サマリー（年月・スタッフ別）

    定義:
      売上      = その月に入金済み一覧に入った金額（入金日がその月の承認済み金額）
      見込み売上 = 申込一覧表（その月の申込）の入金予定金額の合計（満額）
      未入金    = 未入金一覧（その月以前の申込で未承認の方向）の金額合計
      申込数    = 当月の申込件数（キャンセル除く）
      契約数    = ステータス「契約」件数
      ライフライン/火災保険/引越し = 各付帯件数と申込数に対する割合(%)
    """
    allowed = get_allowed_store_ids()
    store_id = request.args.get('store_id', type=int)
    staff_id = request.args.get('staff_id', type=int)
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]

    import calendar as _cal
    month_end = date(year, month, _cal.monthrange(year, month)[1])

    base = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed),
        ~ApplicationRecord.status.in_(['キャンセル', 'キャンセル振替']),
    )
    if store_id and store_id in allowed:
        base = base.filter(ApplicationRecord.store_id == store_id)
    if staff_id:
        base = base.filter(ApplicationRecord.staff_id == staff_id)

    # その月の申込（件数・付帯・見込み売上）
    month_recs = base.filter(
        db.extract('year',  ApplicationRecord.application_date) == year,
        db.extract('month', ApplicationRecord.application_date) == month,
    ).all()

    # 売上：入金日がその月の承認済み金額（申込月は問わない）
    paid_recs = base.all()
    sales = 0
    for r in paid_recs:
        if _approved_in_month(r, 'brokerage', year, month): sales += (r.brokerage_fee or 0)
        if _approved_in_month(r, 'option', year, month):    sales += (r.option_amount or 0)
        if _approved_in_month(r, 'ad', year, month):         sales += _ad_yen(r)

    # 未入金：その月以前の申込で未承認の方向の金額合計
    uncollected = 0
    for r in base.filter(ApplicationRecord.application_date <= month_end).all():
        if (r.brokerage_fee or 0) != 0 and not r.brokerage_approved: uncollected += r.brokerage_fee or 0
        if (r.option_amount or 0) != 0 and not r.option_approved:    uncollected += r.option_amount or 0
        ady = _ad_yen(r)
        if ady != 0 and not r.ad_approved:                            uncollected += ady

    expected_sales = sum(_record_total_amount(r) for r in month_recs)
    app_count    = len(month_recs)
    contract_count = sum(1 for r in month_recs if r.status == '契約')
    lifeline_count = sum(1 for r in month_recs if r.lifeline)
    moving_count   = sum(1 for r in month_recs if r.moving)
    fire_count     = sum(1 for r in month_recs if r.fire_insurance)
    rate = lambda c: round(c / app_count * 100, 1) if app_count else 0

    return jsonify({
        'sales': sales,
        'expected_sales': expected_sales,
        'uncollected': uncollected,
        'application_count': app_count,
        'contract_count': contract_count,
        'lifeline_count': lifeline_count,
        'moving_count': moving_count,
        'fire_insurance_count': fire_count,
        'lifeline_rate': rate(lifeline_count),
        'moving_rate': rate(moving_count),
        'fire_insurance_rate': rate(fire_count),
    })


@app.route("/api/applications/search")
@login_required
def api_applications_search():
    """全期間を横断して申込レコードをキーワード検索"""
    q = request.args.get('q', '').strip()
    if not q or len(q) < 1:
        return jsonify([])

    allowed_ids = get_allowed_store_ids()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    like = f'%{q}%'

    # スタッフ名検索のためのID収集
    matching_staff_ids = [s.id for s in Staff.query.filter(Staff.name.ilike(like)).all()]

    conditions = [
        ApplicationRecord.property_name.ilike(like),
        ApplicationRecord.customer_name.ilike(like),
        ApplicationRecord.room_number.ilike(like),
        ApplicationRecord.media.ilike(like),
    ]
    if matching_staff_ids:
        conditions.append(ApplicationRecord.staff_id.in_(matching_staff_ids))

    query = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        db.or_(*conditions)
    )
    # スタッフも他スタッフの情報を横断検索可（編集は本人のみ）

    records = query.order_by(ApplicationRecord.application_date.desc(), ApplicationRecord.id.desc()).limit(200).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify([_app_record_to_dict(r, staff_map) for r in records])


@app.route("/api/applications/management-companies")
@login_required
def api_management_companies():
    """過去に入力された管理会社名の一覧（入力候補＝記憶用）"""
    allowed_ids = get_allowed_store_ids()
    rows = db.session.query(ApplicationRecord.management_company).filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ApplicationRecord.management_company.isnot(None),
        ApplicationRecord.management_company != '',
    ).distinct().all()
    names = sorted({(r[0] or '').strip() for r in rows if (r[0] or '').strip()})
    return jsonify(names)


@app.route("/api/applications", methods=["GET"])
@login_required
def api_applications_list():
    allowed_ids = get_allowed_store_ids()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    staff_id_filter = request.args.get('staff_id', type=int)
    status_filter = request.args.get('status')

    q = ApplicationRecord.query.filter(ApplicationRecord.store_id.in_(allowed_ids))

    # スタッフも他スタッフの情報を閲覧可（編集は本人のみ＝フロント/更新APIで制御）
    if staff_id_filter:
        q = q.filter(ApplicationRecord.staff_id == staff_id_filter)

    if year and month:
        from sqlalchemy import extract
        q = q.filter(
            extract('year', ApplicationRecord.application_date) == year,
            extract('month', ApplicationRecord.application_date) == month
        )

    if status_filter:
        q = q.filter(ApplicationRecord.status == status_filter)

    # 申込一覧は全件表示（全部入金済みになっても申込一覧からは消さない）
    records = q.order_by(ApplicationRecord.application_date.asc(), ApplicationRecord.id.asc()).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify([_app_record_to_dict(r, staff_map) for r in records])


@app.route("/api/applications", methods=["POST"])
@login_required
def api_applications_create():
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    data = request.get_json() or {}

    req_sid = data.get('store_id')
    if req_sid and int(req_sid) in allowed_ids:
        store_id = int(req_sid)
    elif allowed_ids:
        store_id = allowed_ids[0]
    else:
        return jsonify({'error': '利用可能な店舗がありません'}), 403

    staff_id = data.get('staff_id') or None
    if cur_user and cur_user.role == 'staff':
        staff_id = cur_user.staff_id  # staff は自分のレコードのみ

    rec = ApplicationRecord(
        store_id=store_id, staff_id=staff_id,
        application_date=_parse_date(data.get('application_date')) or date.today(),
        media=data.get('media') or None,
        property_name=data.get('property_name') or None,
        room_number=data.get('room_number') or None,
        customer_name=data.get('customer_name') or None,
        rent=float(data.get('rent') or 0),
        management_company=data.get('management_company') or None,
        review_ng=bool(data.get('review_ng')),
        contract_start_date=_parse_date(data.get('contract_start_date')),
        ad_payment_date=_parse_date(data.get('ad_payment_date')),
        brokerage_fee=float(data.get('brokerage_fee') or 0),
        option_amount=float(data.get('option_amount') or 0),
        ad_type=data.get('ad_type') or 'percent',
        ad_amount=float(data.get('ad_amount') or 0),
        lifeline=bool(data.get('lifeline')),
        moving=bool(data.get('moving')),
        fire_insurance=bool(data.get('fire_insurance')),
        status=data.get('status') or '申込',
    )
    db.session.add(rec)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': rec.id})


@app.route("/api/applications/<int:rid>", methods=["GET"])
@login_required
def api_applications_get(rid):
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify(_app_record_to_dict(rec, staff_map))


@app.route("/api/applications/<int:rid>", methods=["PUT"])
@login_required
def api_applications_update(rid):
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    if cur_user and cur_user.role == 'staff' and rec.staff_id != cur_user.staff_id:
        return jsonify({'error': '権限がありません'}), 403

    data = request.get_json() or {}
    is_manager = cur_user and cur_user.role in ('owner', 'store_manager', 'super_admin')

    for fld in ['media', 'property_name', 'room_number', 'customer_name', 'status', 'ad_type', 'management_company']:
        if fld in data: setattr(rec, fld, data[fld] or None)
    if 'staff_id' in data and is_manager:
        rec.staff_id = data['staff_id'] or None
    for fld in ['rent', 'brokerage_fee', 'ad_amount', 'option_amount']:
        if fld in data: setattr(rec, fld, float(data[fld] or 0))
    for fld in ['lifeline', 'moving', 'fire_insurance']:
        if fld in data: setattr(rec, fld, bool(data[fld]))
    for fld in ['application_date', 'contract_start_date', 'ad_payment_date', 'brokerage_payment_date', 'option_payment_date']:
        if fld in data: setattr(rec, fld, _parse_date(data[fld]))

    # 審査状態: None=— / 'ok'=○→契約 / 'ng'=×→キャンセル
    if 'review_status' in data:
        rs = data['review_status'] or None
        rec.review_status = rs
        rec.review_ng = (rs == 'ng')
        if rs == 'ok':
            rec.status = '契約'
        elif rs == 'ng':
            rec.status = 'キャンセル'
        else:
            # リセット: 審査由来のステータスを申込に戻す
            if rec.status in ('契約', 'キャンセル'):
                rec.status = '申込'

    rec.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/applications/<int:rid>", methods=["DELETE"])
@login_required
def api_applications_delete(rid):
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    if cur_user and cur_user.role == 'staff':
        return jsonify({'error': '削除権限がありません'}), 403
    db.session.delete(rec)
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/applications/<int:rid>/settle", methods=["POST"])
@login_required
def api_applications_settle(rid):
    """営業マンが入金報告（決済フラグ）"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    if cur_user and cur_user.role == 'staff' and rec.staff_id != cur_user.staff_id:
        return jsonify({'error': '権限がありません'}), 403

    data = request.get_json() or {}
    field = data.get('field')  # 'ad' / 'brokerage' / 'option'
    pay_date = _parse_date(data.get('date'))  # 報告時に入力された入金日
    if field == 'ad':
        rec.ad_settled = True
        if pay_date: rec.ad_payment_date = pay_date
    elif field == 'brokerage':
        rec.brokerage_settled = True
        if pay_date: rec.brokerage_payment_date = pay_date
    elif field == 'option':
        rec.option_settled = True
        if pay_date: rec.option_payment_date = pay_date
    else:
        return jsonify({'error': 'invalid field'}), 400
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/applications/<int:rid>/approve", methods=["POST"])
@login_required
def api_applications_approve(rid):
    """店長が入金を承認 → 全項目承認済みになったらSalesKPIの売上に反映"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403

    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403

    data = request.get_json() or {}
    field = data.get('field')  # 'ad' / 'brokerage' / 'option'

    # 承認日（入金日）を指定可能。未指定なら当日
    approve_date = _parse_date(data.get('date')) or date.today()

    if field == 'ad' and not rec.ad_approved:
        rec.ad_approved = True
        rec.ad_payment_date = rec.ad_payment_date or approve_date
    elif field == 'brokerage' and not rec.brokerage_approved:
        rec.brokerage_approved = True
        rec.brokerage_payment_date = rec.brokerage_payment_date or approve_date
    elif field == 'option' and not rec.option_approved:
        rec.option_approved = True
        rec.option_payment_date = rec.option_payment_date or approve_date
    else:
        return jsonify({'error': 'invalid field or already approved'}), 400

    # 承認されたフィールド分だけ即座に売上に反映（部分承認対応）
    # 仲手・その他費用・ADはそれぞれ独立した入金方向として加算する
    ad_yen = round((rec.rent or 0) * (rec.ad_amount or 0) / 100) if (rec.ad_type or 'amount') == 'percent' else (rec.ad_amount or 0)

    approved_amount = 0
    if field == 'brokerage':
        approved_amount += (rec.brokerage_fee or 0)
    elif field == 'option':
        approved_amount += (rec.option_amount or 0)
    elif field == 'ad' and ad_yen > 0:
        approved_amount += ad_yen

    if approved_amount > 0 and rec.staff_id:
        # staff_idが未設定の場合はKPI反映をスキップ（IntegrityError防止）
        ref_date = rec.application_date or date.today()
        ref_year, ref_month = ref_date.year, ref_date.month
        kpi = SalesKPI.query.filter_by(
            staff_id=rec.staff_id, store_id=rec.store_id,
            year=ref_year, month=ref_month
        ).first()
        if not kpi:
            kpi = SalesKPI(staff_id=rec.staff_id, store_id=rec.store_id,
                           year=ref_year, month=ref_month)
            db.session.add(kpi)
        kpi.sales_amount = (kpi.sales_amount or 0) + approved_amount

    db.session.commit()
    return jsonify({'status': 'ok', 'approved_amount': approved_amount})


@app.route("/api/applications/<int:rid>/unapprove", methods=["POST"])
@login_required
def api_applications_unapprove(rid):
    """店長が入金承認を取消 → その方向を未報告に戻し、売上(KPI)から差し引く"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403

    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403

    data = request.get_json() or {}
    field = data.get('field')  # 'ad' / 'brokerage' / 'option'
    ad_yen = round((rec.rent or 0) * (rec.ad_amount or 0) / 100) if (rec.ad_type or 'amount') == 'percent' else (rec.ad_amount or 0)

    removed_amount = 0
    if field == 'brokerage' and rec.brokerage_approved:
        rec.brokerage_approved = False; rec.brokerage_settled = False
        rec.brokerage_payment_date = None
        removed_amount = rec.brokerage_fee or 0
    elif field == 'option' and rec.option_approved:
        rec.option_approved = False; rec.option_settled = False
        rec.option_payment_date = None
        removed_amount = rec.option_amount or 0
    elif field == 'ad' and rec.ad_approved:
        rec.ad_approved = False; rec.ad_settled = False
        rec.ad_payment_date = None
        removed_amount = ad_yen
    else:
        return jsonify({'error': 'invalid field or not approved'}), 400

    # 承認時に加算したKPI売上を差し引く
    if removed_amount and rec.staff_id:
        ref_date = rec.application_date or date.today()
        kpi = SalesKPI.query.filter_by(
            staff_id=rec.staff_id, store_id=rec.store_id,
            year=ref_date.year, month=ref_date.month
        ).first()
        if kpi:
            kpi.sales_amount = (kpi.sales_amount or 0) - removed_amount

    db.session.commit()
    return jsonify({'status': 'ok', 'removed_amount': removed_amount})


@app.route("/api/applications/<int:rid>/unsettle", methods=["POST"])
@login_required
def api_applications_unsettle(rid):
    """否認：入金報告を取消して未報告に戻す（承認前のみ／スタッフは自分の案件のみ可）"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    rec = ApplicationRecord.query.get_or_404(rid)
    if rec.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403
    if cur_user and cur_user.role == 'staff' and rec.staff_id != cur_user.staff_id:
        return jsonify({'error': '権限がありません'}), 403

    data = request.get_json() or {}
    field = data.get('field')  # 'ad' / 'brokerage' / 'option'
    if field == 'brokerage':
        if rec.brokerage_approved:
            return jsonify({'error': 'already approved'}), 400
        rec.brokerage_settled = False; rec.brokerage_payment_date = None
    elif field == 'option':
        if rec.option_approved:
            return jsonify({'error': 'already approved'}), 400
        rec.option_settled = False; rec.option_payment_date = None
    elif field == 'ad':
        if rec.ad_approved:
            return jsonify({'error': 'already approved'}), 400
        rec.ad_settled = False; rec.ad_payment_date = None
    else:
        return jsonify({'error': 'invalid field'}), 400
    db.session.commit()
    return jsonify({'status': 'ok'})


@app.route("/api/pending-approvals")
@login_required
def api_pending_approvals():
    """店長向け：承認待ち件数"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'count': 0})
    allowed_ids = get_allowed_store_ids()
    count = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        db.or_(
            db.and_(ApplicationRecord.ad_settled == True, ApplicationRecord.ad_approved == False),
            db.and_(ApplicationRecord.brokerage_settled == True, ApplicationRecord.brokerage_approved == False),
            db.and_(ApplicationRecord.option_settled == True, ApplicationRecord.option_approved == False)
        )
    ).count()
    return jsonify({'count': count})


# ── 媒体マスター API ──────────────────────────────────────

@app.route("/api/media-types", methods=["GET"])
@login_required
def api_media_types_list():
    allowed_ids = get_allowed_store_ids()
    store_id = allowed_ids[0] if allowed_ids else 1
    items = MediaType.query.filter_by(store_id=store_id, is_active=True)\
        .order_by(MediaType.sort_order, MediaType.name).all()
    return jsonify([{'id': m.id, 'name': m.name} for m in items])


@app.route("/api/media-types", methods=["POST"])
@login_required
def api_media_types_create():
    allowed_ids = get_allowed_store_ids()
    store_id = allowed_ids[0] if allowed_ids else 1
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '媒体名を入力してください'}), 400
    if MediaType.query.filter_by(store_id=store_id, name=name, is_active=True).first():
        return jsonify({'error': 'すでに存在します'}), 400
    m = MediaType(store_id=store_id, name=name)
    db.session.add(m)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': m.id, 'name': m.name})


@app.route("/api/media-types/<int:mid>", methods=["DELETE"])
@login_required
def api_media_types_delete(mid):
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    m = MediaType.query.get_or_404(mid)
    m.is_active = False
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── ステータスカラー API ──────────────────────────────────

STATUS_COLOR_DEFAULTS = {
    '申込':          {'bg': '#ffffff', 'text': '#111827', 'row_bg': '#ffffff'},
    '契約':          {'bg': '#fef9c3', 'text': '#92400e', 'row_bg': '#fef9c3'},
    'キャンセル':     {'bg': '#fee2e2', 'text': '#b91c1c', 'row_bg': '#e5e7eb'},
    'キャンセル振替': {'bg': '#dcfce7', 'text': '#15803d', 'row_bg': '#e5e7eb'},
}


@app.route("/api/status-colors", methods=["GET"])
@login_required
def api_status_colors_get():
    allowed_ids = get_allowed_store_ids()
    store_id = allowed_ids[0] if allowed_ids else 1
    result = {}
    for key, default in STATUS_COLOR_DEFAULTS.items():
        sc = StatusColor.query.filter_by(store_id=store_id, status_key=key).first()
        result[key] = {
            'bg':     sc.bg_color if sc else default['bg'],
            'text':   sc.text_color if sc else default['text'],
            'row_bg': (sc.row_bg_color if (sc and sc.row_bg_color) else default.get('row_bg', '#ffffff')),
        }
    return jsonify(result)


@app.route("/api/status-colors", methods=["PUT"])
@login_required
def api_status_colors_update():
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    allowed_ids = get_allowed_store_ids()
    data = request.get_json() or {}
    # フロントは {store_id, colors:{...}} の形で送る。colors を取り出す（後方互換で直接形も許容）
    req_sid = data.get('store_id')
    store_id = int(req_sid) if (req_sid and int(req_sid) in allowed_ids) else (allowed_ids[0] if allowed_ids else 1)
    colors_data = data.get('colors') if isinstance(data.get('colors'), dict) else data
    for status_key, colors in colors_data.items():
        if not isinstance(colors, dict):
            continue   # store_id 等の非カラー項目はスキップ
        sc = StatusColor.query.filter_by(store_id=store_id, status_key=status_key).first()
        if not sc:
            sc = StatusColor(store_id=store_id, status_key=status_key)
            db.session.add(sc)
        sc.bg_color = colors.get('bg', '#ffffff')
        sc.text_color = colors.get('text', '#111827')
        if 'row_bg' in colors:
            sc.row_bg_color = colors.get('row_bg') or '#ffffff'
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── 目標売上 API ─────────────────────────────────────────

@app.route("/api/sales-kpi/target", methods=["POST"])
@login_required
def api_sales_kpi_target():
    """目標売上を設定（staff は自分のみ、manager は全員）"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    data = request.get_json() or {}

    staff_id = data.get('staff_id')
    if cur_user and cur_user.role == 'staff':
        staff_id = cur_user.staff_id

    year = data.get('year')
    month = data.get('month')
    target = float(data.get('target_sales') or 0)

    if not staff_id or not year or not month:
        return jsonify({'error': 'パラメータ不足'}), 400

    staff = Staff.query.get(staff_id)
    if not staff or staff.store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403

    kpi = SalesKPI.query.filter_by(staff_id=staff_id, year=year, month=month,
                                    store_id=staff.store_id).first()
    if not kpi:
        kpi = SalesKPI(staff_id=staff_id, store_id=staff.store_id, year=year, month=month)
        db.session.add(kpi)
    kpi.target_sales = target
    db.session.commit()
    return jsonify({'status': 'ok'})


# ── プレミアム：本部ダッシュボード ────────────────────────

@app.route("/headquarters")
@login_required
@block_super_admin
def headquarters_dashboard():
    """本部ダッシュボード（プレミアプラン専用）"""
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user:
        return redirect(url_for('app_login'))
    if cur_user.role == 'staff':
        return redirect(url_for('sales_management'))
    # プレミアプランのみ許可
    if not is_premium_user():
        return redirect(url_for('executive_dashboard'))
    stores = get_allowed_stores(ignore_active=True)   # HQは全店舗（active_store_id無視）
    year, month = current_ym()
    return render_template("headquarters_dashboard.html",
                           stores=stores, year=year, month=month,
                           now=datetime.now())


@app.route("/api/hq/summary")
@login_required
@block_super_admin
def api_hq_summary():
    """全店舗KPIサマリー（本部ダッシュボード用・プレミアム限定）"""
    if not is_premium_user():
        return jsonify({'error': 'premium required'}), 403
    year  = request.args.get('year',  type=int) or date.today().year
    month = request.args.get('month', type=int) or date.today().month
    store_ids = get_allowed_store_ids(ignore_active=True)

    today = date.today()
    days_in_month = (date(year, month % 12 + 1, 1) - timedelta(days=1)).day if month < 12 else 31
    elapsed_days  = min(today.day, days_in_month) if (today.year == year and today.month == month) else days_in_month

    result = []
    total_sales = 0
    total_target = 0
    total_apps   = 0
    total_contracts = 0

    for sid in store_ids:
        store = Store.query.get(sid)
        if not store:
            continue
        kpis = SalesKPI.query.filter_by(store_id=sid, year=year, month=month).all()
        sales    = sum(k.sales_amount  or 0 for k in kpis)
        target   = sum(k.target_sales  or 0 for k in kpis)
        apps     = sum(k.applications  or 0 for k in kpis)
        contracts= sum(k.contracts     or 0 for k in kpis)
        inquiries= sum(k.inquiries     or 0 for k in kpis)

        # 前月売上
        pm = month - 1 if month > 1 else 12
        py = year if month > 1 else year - 1
        prev_kpis = SalesKPI.query.filter_by(store_id=sid, year=py, month=pm).all()
        prev_sales = sum(k.sales_amount or 0 for k in prev_kpis)

        # 着地予測（現ペース × 月日数）
        forecast = int(sales / elapsed_days * days_in_month) if elapsed_days > 0 else 0

        close_rate = round(contracts / apps * 100, 1) if apps > 0 else 0
        vs_prev    = round((sales - prev_sales) / prev_sales * 100, 1) if prev_sales > 0 else None

        total_sales    += sales
        total_target   += target
        total_apps     += apps
        total_contracts += contracts

        # 利益・経費はPLRecordから取得
        pl = PLRecord.query.filter_by(store_id=sid, year=year, month=month).first()
        profit   = pl.net_profit if pl else None
        expenses = max(0, sales - (profit or 0)) if profit is not None else None

        result.append({
            'store_id':   sid,
            'store_name': store.name,
            'sales':      sales,
            'target':     target,
            'forecast':   forecast,
            'apps':       apps,
            'contracts':  contracts,
            'inquiries':  inquiries,
            'close_rate': close_rate,
            'vs_prev':    vs_prev,
            'prev_sales': prev_sales,
            'profit':     profit,
            'expenses':   expenses,
            'is_danger':  (target > 0 and sales < target * 0.5),
            'is_drop':    (vs_prev is not None and vs_prev <= -20),
        })

    total_forecast  = int(total_sales / elapsed_days * days_in_month) if elapsed_days > 0 else 0
    total_close     = round(total_contracts / total_apps * 100, 1) if total_apps > 0 else 0
    total_inquiries = sum(r['inquiries'] for r in result)
    total_profit    = sum(r['profit']    for r in result if r['profit'] is not None)
    total_expenses  = sum(r['expenses']  for r in result if r['expenses'] is not None)
    return jsonify({
        'stores':           result,
        'total_sales':      total_sales,
        'total_target':     total_target,
        'total_forecast':   total_forecast,
        'total_apps':       total_apps,
        'total_contracts':  total_contracts,
        'total_inquiries':  total_inquiries,
        'total_profit':     total_profit,
        'total_expenses':   total_expenses,
        'total_close_rate': total_close,
        'year': year, 'month': month,
        'elapsed_days': elapsed_days, 'days_in_month': days_in_month,
    })


@app.route("/api/hq/rankings")
@login_required
@block_super_admin
def api_hq_rankings():
    """店舗別ランキング（metric: sales / apps / close_rate・プレミアム限定）"""
    if not is_premium_user():
        return jsonify({'error': 'premium required'}), 403
    year   = request.args.get('year',   type=int) or date.today().year
    month  = request.args.get('month',  type=int) or date.today().month
    metric = request.args.get('metric', 'sales')
    store_ids = get_allowed_store_ids(ignore_active=True)

    rows = []
    for sid in store_ids:
        store = Store.query.get(sid)
        if not store:
            continue
        kpis = SalesKPI.query.filter_by(store_id=sid, year=year, month=month).all()
        sales     = sum(k.sales_amount or 0 for k in kpis)
        apps      = sum(k.applications or 0 for k in kpis)
        contracts = sum(k.contracts    or 0 for k in kpis)
        inquiries = sum(k.inquiries    or 0 for k in kpis)
        close_rate= round(contracts / apps * 100, 1) if apps > 0 else 0
        pl = PLRecord.query.filter_by(store_id=sid, year=year, month=month).first()
        profit   = pl.net_profit if pl else 0
        expenses = max(0, sales - profit) if pl else 0
        rows.append({'store_id': sid, 'store_name': store.name,
                     'sales': sales, 'apps': apps, 'contracts': contracts,
                     'inquiries': inquiries, 'close_rate': close_rate,
                     'profit': profit, 'expenses': expenses})

    key_map = {'sales':'sales','apps':'apps','contracts':'contracts',
               'inquiries':'inquiries','close_rate':'close_rate',
               'profit':'profit','expenses':'expenses'}
    sort_key = key_map.get(metric, 'sales')
    rows.sort(key=lambda r: r.get(sort_key, 0), reverse=True)
    for i, r in enumerate(rows):
        r['rank'] = i + 1
    return jsonify(rows)


@app.route("/api/hq/store-comparison")
@login_required
@block_super_admin
def api_hq_store_comparison():
    """店舗別月次推移（グラフ用・プレミアム限定）"""
    if not is_premium_user():
        return jsonify({'error': 'premium required'}), 403
    from_param = request.args.get('from')
    to_param   = request.args.get('to')
    metric     = request.args.get('metric', 'sales')
    store_ids  = get_allowed_store_ids(ignore_active=True)

    today = date.today()
    if from_param and to_param:
        try:
            fy, fm = int(from_param[:4]), int(from_param[5:7])
            ty, tm = int(to_param[:4]),   int(to_param[5:7])
        except Exception:
            fy, fm = today.year, today.month - 5
            ty, tm = today.year, today.month
    else:
        base = today.year * 12 + today.month - 1
        fy, fm = divmod(base - 5, 12)
        fm += 1
        ty, tm = today.year, today.month

    base_s = fy * 12 + fm - 1
    base_e = ty * 12 + tm - 1
    periods = [(t // 12, t % 12 + 1) for t in range(base_s, base_e + 1)]
    labels  = [f'{y}/{m:02d}' for y, m in periods]

    datasets = []
    colors_list = ['#16a34a','#2563eb','#dc2626','#d97706','#7c3aed','#db2777','#0891b2']
    for ci, sid in enumerate(store_ids):
        store = Store.query.get(sid)
        if not store:
            continue
        values = []
        for y, m in periods:
            kpis = SalesKPI.query.filter_by(store_id=sid, year=y, month=m).all()
            if metric == 'sales':
                v = sum(k.sales_amount or 0 for k in kpis)
            elif metric == 'apps':
                v = sum(k.applications or 0 for k in kpis)
            elif metric == 'contracts':
                v = sum(k.contracts    or 0 for k in kpis)
            elif metric == 'inquiries':
                v = sum(k.inquiries    or 0 for k in kpis)
            elif metric == 'close_rate':
                apps = sum(k.applications or 0 for k in kpis)
                ctrs = sum(k.contracts   or 0 for k in kpis)
                v = round(ctrs / apps * 100, 1) if apps > 0 else 0
            elif metric in ('profit', 'expenses'):
                pl = PLRecord.query.filter_by(store_id=sid, year=y, month=m).first()
                sales_v = sum(k.sales_amount or 0 for k in kpis)
                profit_v = pl.net_profit if pl else 0
                v = profit_v if metric == 'profit' else max(0, sales_v - profit_v)
            else:
                v = 0
            values.append(v)
        datasets.append({
            'store_id':   sid,
            'store_name': store.name,
            'color':      colors_list[ci % len(colors_list)],
            'data':       values,
        })
    return jsonify({'labels': labels, 'datasets': datasets})


@app.route("/api/hq/ai-summary", methods=["POST"])
@login_required
def api_hq_ai_summary():
    """Claude APIで全店舗データのAIサマリーを生成"""
    if not is_premium_user():
        return jsonify({'error': 'premium required'}), 403
    year  = request.json.get('year',  date.today().year)
    month = request.json.get('month', date.today().month)
    store_ids = get_allowed_store_ids(ignore_active=True)

    store_lines = []
    for sid in store_ids:
        store = Store.query.get(sid)
        if not store:
            continue
        kpis = SalesKPI.query.filter_by(store_id=sid, year=year, month=month).all()
        sales     = sum(k.sales_amount or 0 for k in kpis)
        target    = sum(k.target_sales or 0 for k in kpis)
        apps      = sum(k.applications or 0 for k in kpis)
        contracts = sum(k.contracts    or 0 for k in kpis)
        close     = round(contracts / apps * 100, 1) if apps > 0 else 0
        store_lines.append(
            f"・{store.name}: 売上¥{sales:,.0f} (目標¥{target:,.0f}), 申込{apps}件, 成約{contracts}件, 成約率{close}%"
        )

    prompt = f"""あなたは不動産会社の経営コンサルタントです。
以下は{year}年{month}月の全店舗KPIデータです。

{chr(10).join(store_lines) if store_lines else '（データなし）'}

以下の観点で簡潔に分析してください（400字以内）：
1. 全体の状況と注目すべき店舗
2. 課題・リスク
3. 来月に向けたアドバイス"""

    try:
        msg = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return jsonify({'summary': msg.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# 反響メール自動取込サービス（IMAP IDLE）を起動（複数ワーカーでも1つのみ）
try:
    start_mail_service()
except Exception as _e:
    print(f"start_mail_service error: {_e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    host = "0.0.0.0" if _IS_POSTGRES else "127.0.0.1"
    app.run(debug=False, port=port, host=host, use_reloader=False)
