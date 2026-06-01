# ======================================================
# DBリセット手順:
#   PLRecordモデルに新カラムを追加したため、
#   既存のDBには新カラムが存在しない。
#   リセットするには instance/realestate.db を削除して再起動する。
#   例: del instance\realestate.db (Windows)
# ======================================================
import os
import random
import tempfile
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
    """HTMLページのブラウザキャッシュを無効化"""
    if 'text/html' in response.content_type:
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
    bg_color = db.Column(db.String(20), default='#ffffff')
    text_color = db.Column(db.String(20), default='#111827')


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
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)


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

    # app_user の権限フラグカラムを追加
    cursor.execute("PRAGMA table_info(app_user)")
    au_cols = {r[1] for r in cursor.fetchall()}
    for col_name, col_def in [
        ('can_view_accounting',    'INTEGER DEFAULT 1'),
        ('can_view_all_staff',     'INTEGER DEFAULT 1'),
        ('can_edit_kpi',           'INTEGER DEFAULT 1'),
        ('can_manage_uncollected', 'INTEGER DEFAULT 1'),
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
    ]:
        if col_name not in ar_cols:
            try:
                cursor.execute(f"ALTER TABLE application_record ADD COLUMN {col_name} {col_def}")
                print(f"  Added column application_record.{col_name}")
            except Exception as e:
                print(f"  Skip application_record.{col_name}: {e}")

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
        ("daily_report",             "store_id",     "INTEGER"),
        ("customer_service_record",  "status",       "VARCHAR(20) DEFAULT '追客中'"),
        ("tenant", "trial_ends_at",        "TIMESTAMP"),
        ("tenant", "subscription_status",  "VARCHAR(20) DEFAULT 'trial'"),
        ("tenant", "contract_start_date",     "DATE"),
        ("store",  "created_at",             "TIMESTAMP"),
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
            # super_admin はテナント管理へ、それ以外は売上管理ダッシュボードへ
            if user.role == 'super_admin':
                dashboard_url = url_for('admin_tenants')
            elif user.role == 'staff':
                dashboard_url = url_for('sales_management')
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
    stores = get_allowed_stores(ignore_active=True)
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all()
    year, month = current_ym()
    store_id = allowed_ids[0] if allowed_ids else None
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
    return render_template("customer_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now(),
                           cur_role=cur_role, cur_staff_id=cur_staff_id,
                           is_manager=is_manager, media_types=media_types,
                           store_id=store_id)


@app.route("/echo-management")
@login_required
@block_super_admin
def echo_management():
    """反響管理表ページ"""
    stores = get_allowed_stores(ignore_active=True)
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all() if allowed_ids else []
    year, month = current_ym()
    store_id = allowed_ids[0] if allowed_ids else None
    return render_template("echo_management.html",
                           stores=stores, staff_list=staff_list,
                           year=year, month=month, store_id=store_id,
                           now=datetime.now())


@app.route("/api/echo-records", methods=["GET"])
@login_required
def api_echo_records_list():
    allowed = get_allowed_store_ids(ignore_active=True)
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
    allowed = get_allowed_store_ids(ignore_active=True)
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

    r.staff_id = int(data.get('staff_id') or 0) or None
    r.list_name = data.get('list_name', r.list_name)
    r.echo_date = pd(data.get('echo_date')) or r.echo_date
    r.media  = data.get('media', r.media)
    r.method = data.get('method', r.method)
    r.first_contact_date = pd(data.get('first_contact_date'))
    for i in range(1, 11):
        setattr(r, f'followup_{i}', pd(data.get(f'followup_{i}')))
    r.has_reply = bool(data.get('has_reply'))
    r.has_phone = bool(data.get('has_phone'))
    r.has_line  = bool(data.get('has_line'))
    r.memo = data.get('memo', r.memo)
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
    stores = get_allowed_stores(ignore_active=True)
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all() if allowed_ids else []
    year, month = current_ym()
    store_id = allowed_ids[0] if allowed_ids else None
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
    allowed = get_allowed_store_ids(ignore_active=True)
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
    allowed = get_allowed_store_ids(ignore_active=True)
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


@app.route("/api/leads/monthly-stats")
@login_required
def api_leads_monthly_stats():
    """媒体別月次反響統計を返す"""
    cy, cm = current_ym()
    year  = request.args.get('year',  type=int) or cy
    month = request.args.get('month', type=int) or cm
    # ignore_active=True で保存時と同じ基準で店舗を解決する
    allowed = get_allowed_store_ids(ignore_active=True)
    if not allowed:
        return jsonify({'stats': [], 'totals': {}, 'trend': []}), 200
    req_sid = request.args.get('store_id', type=int) or 0
    store_id = req_sid if req_sid and req_sid in allowed else allowed[0]

    stats = LeadMediaStat.query.filter_by(store_id=store_id, year=year, month=month).all()

    # 前月比較
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    prev_stats = LeadMediaStat.query.filter_by(store_id=store_id, year=prev_y, month=prev_m).all()
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
        ms = LeadMediaStat.query.filter_by(store_id=store_id, year=ty, month=tm).all()
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
    allowed = get_allowed_store_ids(ignore_active=True)
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
        prev_pls = PLRecord.query.filter_by(year=y, month=m).all()
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

    if store_id:
        query = query.filter_by(store_id=store_id)
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
    staff_list = Staff.query.filter_by(is_active=True).all()
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
    staff_list = Staff.query.filter_by(is_active=True).all()
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
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(Staff.store_id.in_(allowed_ids), Staff.is_active == True).all() if allowed_ids else Staff.query.filter_by(is_active=True).all()
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
                           is_owner=is_owner, is_manager=is_manager,
                           current_user=user,
                           now=datetime.now())


@app.route("/api/settings/staff/add", methods=["POST"])
@login_required
def api_settings_staff_add():
    """スタッフ追加"""
    data = request.get_json() or request.form
    # ignore_active=True で設定ページと同じ基準で店舗を解決する
    allowed = get_allowed_store_ids(ignore_active=True)
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
@app.route("/api/kpi/staff-history")
def api_kpi_staff_history():
    """スタッフ別の6ヶ月推移データを返す"""
    staff_id = request.args.get('staff_id', type=int)
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]

    history = []
    yoy_data = {}
    for i in range(5, -1, -1):
        m = month - i
        y = year
        while m < 1:
            m += 12; y -= 1
        query = SalesKPI.query.filter_by(year=y, month=m)
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
        # 現在月のみ前年比を計算
        if i == 0:
            prev_y_kpis = SalesKPI.query.filter_by(year=y-1, month=m)
            if staff_id:
                prev_y_kpis = prev_y_kpis.filter_by(staff_id=staff_id)
            prev_y_kpis = prev_y_kpis.all()
            yoy_data = {
                'inquiries':    {'cur': sum(k.inquiries    or 0 for k in kpis),   'yoy': sum(k.inquiries    or 0 for k in prev_y_kpis)},
                'applications': {'cur': sum(k.applications or 0 for k in kpis),   'yoy': sum(k.applications or 0 for k in prev_y_kpis)},
                'contracts':    {'cur': sum(k.contracts    or 0 for k in kpis),   'yoy': sum(k.contracts    or 0 for k in prev_y_kpis)},
                'revenue':      {'cur': sum(k.sales_amount or 0 for k in kpis),   'yoy': sum(k.sales_amount or 0 for k in prev_y_kpis)},
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
        ApplicationRecord.status != 'キャンセル',
        db.or_(
            db.and_(ApplicationRecord.ad_amount > 0,       ApplicationRecord.ad_approved == False),
            db.and_(ApplicationRecord.brokerage_fee > 0,   ApplicationRecord.brokerage_approved == False),
        )
    ).all()

    added = 0
    for rec in apps:
        pending_amount = 0
        if (rec.ad_amount or 0) > 0 and not rec.ad_approved:
            pending_amount += rec.ad_amount or 0
        if (rec.brokerage_fee or 0) > 0 and not rec.brokerage_approved:
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
    stores     = get_allowed_stores(ignore_active=True)
    allowed_ids = [s.id for s in stores]
    staff_list = Staff.query.filter(
        Staff.store_id.in_(allowed_ids), Staff.is_active == True
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
    q = LeaveRecord.query.filter(
        db.extract('year', LeaveRecord.leave_date) == year
    )
    if staff_id:
        q = q.filter_by(staff_id=staff_id)
    records = q.order_by(LeaveRecord.leave_date.desc()).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
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
    staff_list = Staff.query.filter_by(is_active=True).all()
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
    """パスワードリセットメールを送信（SMTP設定がある場合のみ）"""
    smtp_host = os.getenv('SMTP_HOST', '')
    smtp_port = int(os.getenv('SMTP_PORT', 587))
    smtp_user = os.getenv('SMTP_USER', '')
    smtp_pass = os.getenv('SMTP_PASS', '')
    from_email = os.getenv('FROM_EMAIL', smtp_user)

    if not smtp_host or not smtp_user:
        return False

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'パスワードリセットのご案内'
        msg['From'] = from_email
        msg['To'] = to_email

        body = f"""パスワードリセットのリクエストを受け付けました。

以下のURLからパスワードをリセットしてください（有効期限: 1時間）:

{reset_url}

このメールに心当たりがない場合は、無視してください。
"""
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, to_email, msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f'メール送信エラー: {e}')
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
            sent = _send_reset_email(user.email, reset_url)
            if not sent:
                # メール送信不可の場合はURLを画面に表示（開発用）
                message = f"（開発環境）リセットURL: {reset_url}"
            else:
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
        return render_template("settings_admin_perms.html",
                               sys_admins=sys_admins,
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

@app.route("/admin/tenants")
@super_admin_required
def admin_tenants():
    """テナント管理ページ（super_adminのみ）"""
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    cur_user = AppUser.query.get(session.get('app_user_id'))
    is_super_admin = cur_user and cur_user.role == 'super_admin'
    return render_template("admin_tenants.html", tenants=tenants, now=datetime.now(),
                           is_super_admin=is_super_admin)


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
    db.session.commit()
    return jsonify({'status': 'ok', 'id': store.id})


@app.route("/api/tenants/<int:tid>/stores/<int:sid>", methods=["PUT"])
@super_admin_required
def api_tenant_store_update(tid, sid):
    """店舗名を変更"""
    store = Store.query.filter_by(id=sid, tenant_id=tid).first_or_404()
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '店舗名は必須です'}), 400
    store.name = name
    db.session.commit()
    return jsonify({'status': 'ok'})


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
        'brokerage_settled': bool(r.brokerage_settled),
        'brokerage_approved': bool(r.brokerage_approved),
        'created_at': r.created_at.isoformat() if r.created_at else None,
    }


@app.route("/api/applications/unpaid")
@login_required
def api_applications_unpaid():
    """未入金一覧：仲介またはADが未承認の申込レコードを返す"""
    allowed_ids = get_allowed_store_ids()
    store_id = request.args.get('store_id', type=int)
    staff_id = request.args.get('staff_id', type=int)

    q = ApplicationRecord.query.filter(
        ApplicationRecord.store_id.in_(allowed_ids),
        ApplicationRecord.status != 'キャンセル',
        db.or_(
            db.and_(ApplicationRecord.brokerage_fee > 0, ApplicationRecord.brokerage_approved == False),
            db.and_(ApplicationRecord.ad_amount > 0,     ApplicationRecord.ad_approved == False),
        )
    )
    if store_id and store_id in allowed_ids:
        q = q.filter(ApplicationRecord.store_id == store_id)
    if staff_id:
        q = q.filter(ApplicationRecord.staff_id == staff_id)

    recs = q.order_by(ApplicationRecord.application_date.asc()).all()
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
        ApplicationRecord.status != 'キャンセル',
        db.extract('year',  ApplicationRecord.application_date) == year,
        db.extract('month', ApplicationRecord.application_date) == month,
        db.or_(ApplicationRecord.ad_amount > 0, ApplicationRecord.brokerage_fee > 0),
        db.or_(ApplicationRecord.ad_amount <= 0, ApplicationRecord.ad_amount == None, ApplicationRecord.ad_approved == True),
        db.or_(ApplicationRecord.brokerage_fee <= 0, ApplicationRecord.brokerage_fee == None, ApplicationRecord.brokerage_approved == True),
    ).all()

    total = sum(
        (r.brokerage_fee or 0) + (r.ad_amount or 0) + (r.option_amount or 0)
        for r in recs
    )
    return jsonify({'total': total, 'count': len(recs)})


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
    if cur_user and cur_user.role == 'staff' and cur_user.staff_id:
        query = query.filter(ApplicationRecord.staff_id == cur_user.staff_id)

    records = query.order_by(ApplicationRecord.application_date.desc()).limit(200).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify([_app_record_to_dict(r, staff_map) for r in records])


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

    if cur_user and cur_user.role == 'staff' and cur_user.staff_id:
        q = q.filter(ApplicationRecord.staff_id == cur_user.staff_id)
    elif staff_id_filter:
        q = q.filter(ApplicationRecord.staff_id == staff_id_filter)

    if year and month:
        from sqlalchemy import extract
        q = q.filter(
            extract('year', ApplicationRecord.application_date) == year,
            extract('month', ApplicationRecord.application_date) == month
        )

    if status_filter:
        q = q.filter(ApplicationRecord.status == status_filter)

    records = q.order_by(ApplicationRecord.application_date.desc()).all()
    staff_map = {s.id: s.name for s in Staff.query.all()}
    return jsonify([_app_record_to_dict(r, staff_map) for r in records])


@app.route("/api/applications", methods=["POST"])
@login_required
def api_applications_create():
    cur_user = AppUser.query.get(session.get('app_user_id'))
    allowed_ids = get_allowed_store_ids()
    data = request.get_json() or {}

    store_id = int(data.get('store_id') or (allowed_ids[0] if allowed_ids else 1))
    if store_id not in allowed_ids:
        return jsonify({'error': '権限がありません'}), 403

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
        contract_start_date=_parse_date(data.get('contract_start_date')),
        ad_payment_date=_parse_date(data.get('ad_payment_date')),
        brokerage_fee=float(data.get('brokerage_fee') or 0),
        option_amount=float(data.get('option_amount') or 0),
        ad_type=data.get('ad_type') or 'amount',
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

    for fld in ['media', 'property_name', 'room_number', 'customer_name', 'status', 'ad_type']:
        if fld in data: setattr(rec, fld, data[fld] or None)
    if 'staff_id' in data and is_manager:
        rec.staff_id = data['staff_id'] or None
    for fld in ['rent', 'brokerage_fee', 'ad_amount', 'option_amount']:
        if fld in data: setattr(rec, fld, float(data[fld] or 0))
    for fld in ['lifeline', 'moving', 'fire_insurance']:
        if fld in data: setattr(rec, fld, bool(data[fld]))
    for fld in ['application_date', 'contract_start_date', 'ad_payment_date']:
        if fld in data: setattr(rec, fld, _parse_date(data[fld]))

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
    field = data.get('field')  # 'ad' or 'brokerage'
    if field == 'ad':
        rec.ad_settled = True
    elif field == 'brokerage':
        rec.brokerage_settled = True
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
    field = data.get('field')  # 'ad' or 'brokerage'

    if field == 'ad' and not rec.ad_approved:
        rec.ad_approved = True
    elif field == 'brokerage' and not rec.brokerage_approved:
        rec.brokerage_approved = True
    else:
        return jsonify({'error': 'invalid field or already approved'}), 400

    # 承認されたフィールド分だけ即座に売上に反映（部分承認対応）
    ad_yen = round((rec.rent or 0) * (rec.ad_amount or 0) / 100) if (rec.ad_type or 'amount') == 'percent' else (rec.ad_amount or 0)
    need_ad        = ad_yen > 0
    need_brokerage = (rec.brokerage_fee or 0) > 0

    # 今回承認したフィールドの金額を加算
    # オプション金額は仲介入金承認時に一緒に反映する
    approved_amount = 0
    if field == 'brokerage' and need_brokerage:
        approved_amount += (rec.brokerage_fee or 0) + (rec.option_amount or 0)
    if field == 'ad' and need_ad:
        approved_amount += ad_yen
        # 仲介がない場合（AD単独）はオプションもAD承認時に反映
        if not need_brokerage:
            approved_amount += (rec.option_amount or 0)

    if approved_amount > 0:
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
            db.and_(ApplicationRecord.brokerage_settled == True, ApplicationRecord.brokerage_approved == False)
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
    '申込':          {'bg': '#ffffff', 'text': '#111827'},
    '契約':          {'bg': '#fef9c3', 'text': '#92400e'},
    'キャンセル':     {'bg': '#fee2e2', 'text': '#b91c1c'},
    'キャンセル振替': {'bg': '#dcfce7', 'text': '#15803d'},
}


@app.route("/api/status-colors", methods=["GET"])
@login_required
def api_status_colors_get():
    allowed_ids = get_allowed_store_ids()
    store_id = allowed_ids[0] if allowed_ids else 1
    result = {}
    for key, default in STATUS_COLOR_DEFAULTS.items():
        sc = StatusColor.query.filter_by(store_id=store_id, status_key=key).first()
        result[key] = {'bg': sc.bg_color if sc else default['bg'],
                       'text': sc.text_color if sc else default['text']}
    return jsonify(result)


@app.route("/api/status-colors", methods=["PUT"])
@login_required
def api_status_colors_update():
    cur_user = AppUser.query.get(session.get('app_user_id'))
    if not cur_user or cur_user.role not in ('owner', 'store_manager', 'super_admin'):
        return jsonify({'error': '権限がありません'}), 403
    allowed_ids = get_allowed_store_ids()
    store_id = allowed_ids[0] if allowed_ids else 1
    data = request.get_json() or {}
    for status_key, colors in data.items():
        sc = StatusColor.query.filter_by(store_id=store_id, status_key=status_key).first()
        if not sc:
            sc = StatusColor(store_id=store_id, status_key=status_key)
            db.session.add(sc)
        sc.bg_color = colors.get('bg', '#ffffff')
        sc.text_color = colors.get('text', '#111827')
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
            'is_danger':  (target > 0 and sales < target * 0.5),
            'is_drop':    (vs_prev is not None and vs_prev <= -20),
        })

    total_forecast = int(total_sales / elapsed_days * days_in_month) if elapsed_days > 0 else 0
    total_close    = round(total_contracts / total_apps * 100, 1) if total_apps > 0 else 0
    return jsonify({
        'stores':          result,
        'total_sales':     total_sales,
        'total_target':    total_target,
        'total_forecast':  total_forecast,
        'total_apps':      total_apps,
        'total_contracts': total_contracts,
        'total_close_rate':total_close,
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
        close_rate= round(contracts / apps * 100, 1) if apps > 0 else 0
        rows.append({'store_id': sid, 'store_name': store.name,
                     'sales': sales, 'apps': apps, 'close_rate': close_rate})

    key_map = {'sales': 'sales', 'apps': 'apps', 'close_rate': 'close_rate'}
    sort_key = key_map.get(metric, 'sales')
    rows.sort(key=lambda r: r[sort_key], reverse=True)
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
            elif metric == 'close_rate':
                apps = sum(k.applications or 0 for k in kpis)
                ctrs = sum(k.contracts   or 0 for k in kpis)
                v = round(ctrs / apps * 100, 1) if apps > 0 else 0
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    host = "0.0.0.0" if _IS_POSTGRES else "127.0.0.1"
    app.run(debug=False, port=port, host=host, use_reloader=False)
