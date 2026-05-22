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
from authlib.integrations.flask_client import OAuth
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


@app.after_request
def add_no_cache(response):
    """HTMLページのブラウザキャッシュを無効化"""
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response
oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── 既存モデル ─────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(200), unique=True, nullable=False)
    email = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(200))
    picture = db.Column(db.String(500))
    plan = db.Column(db.String(50), default="free")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    usage_count = db.Column(db.Integer, default=0)


class AiHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    feature = db.Column(db.String(100))
    input_data = db.Column(db.Text)
    output_data = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ── 幹部向け管理ツール：追加モデル ────────────────────────

class Store(db.Model):
    """店舗マスタ"""
    __tablename__ = 'store'
    id = db.Column(db.Integer, primary_key=True)
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
    # 人件費詳細
    regular_salary = db.Column(db.Float, default=0)    # 正社員給与
    parttime_salary = db.Column(db.Float, default=0)   # アルバイト
    commission_pay = db.Column(db.Float, default=0)    # 歩合給
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


class AppUser(db.Model):
    """管理ツールログインユーザー"""
    __tablename__ = 'app_user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='staff')  # 'owner' or 'staff'
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)


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
    Storeテーブルが空のときだけ「ルームピック」を1店舗、
    スタッフ3名（スタッフ1〜3、役職：営業）を作成する。
    KPI/Lead/PL等のダミーデータは作成しない。
    """
    if Store.query.count() > 0:
        return

    print("初期店舗・スタッフを作成しています...")

    store = Store(
        name='ルームピック',
        is_active=True,
    )
    db.session.add(store)
    db.session.flush()  # IDを確定させる

    for i in range(1, 4):
        staff = Staff(
            name=f'スタッフ{i}',
            store_id=store.id,
            role='営業',
            is_active=True,
        )
        db.session.add(staff)

    db.session.commit()
    print("初期店舗・スタッフの作成が完了しました。")

    # 初期オーナーアカウント作成
    if AppUser.query.count() == 0:
        owner = AppUser(
            username='owner',
            password_hash=generate_password_hash('roompick2024'),
            role='owner'
        )
        db.session.add(owner)
        db.session.commit()
        print("初期オーナーアカウントを作成しました。(username: owner)")
    return


def ensure_owner_account():
    """AppUserが存在しない場合にオーナーアカウントを作成する"""
    if AppUser.query.count() == 0:
        owner = AppUser(
            username='owner',
            password_hash=generate_password_hash('roompick2024'),
            role='owner'
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

    conn.commit()
    conn.close()


with app.app_context():
    db.create_all()
    if not _IS_POSTGRES:   # SQLite（ローカル）のみマイグレーション
        migrate_db()
    init_store()
    ensure_owner_account()


# ── 認証デコレータ ────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'app_user_id' not in session:
            return redirect(url_for('app_login'))
        user = AppUser.query.get(session['app_user_id'])
        if not user or user.role != 'owner':
            return redirect(url_for('executive_dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── ユーティリティ ─────────────────────────────────────────

def get_current_user():
    if "user_id" not in session:
        return None
    return User.query.get(session["user_id"])


def current_ym():
    """現在の年・月をタプルで返す"""
    now = datetime.now()
    return now.year, now.month


# ── 既存ルーティング ──────────────────────────────────────

@app.route("/")
def index():
    # 管理ツールのログインページへリダイレクト
    return redirect(url_for('app_login'))


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    user_info = token.get("userinfo")
    if not user_info:
        return redirect(url_for("index"))

    user = User.query.filter_by(google_id=user_info["sub"]).first()
    if not user:
        user = User(
            google_id=user_info["sub"],
            email=user_info["email"],
            name=user_info.get("name", ""),
            picture=user_info.get("picture", ""),
        )
        db.session.add(user)
        db.session.commit()

    session["user_id"] = user.id
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/app-login", methods=["GET", "POST"])
def app_login():
    """管理ツール専用ログイン"""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = AppUser.query.filter_by(username=username, is_active=True).first()
        if user and check_password_hash(user.password_hash, password):
            session['app_user_id'] = user.id
            session['app_user_role'] = user.role
            session['app_username'] = user.username
            user.last_login = datetime.utcnow()
            db.session.commit()
            return redirect(url_for('executive_dashboard'))
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


@app.route("/dev-login")
def dev_login():
    """開発用：Googleログインなしでテストユーザーとしてログイン"""
    user = User.query.filter_by(google_id="dev-test-user").first()
    if not user:
        user = User(
            google_id="dev-test-user",
            email="demo@ieai.dev",
            name="デモユーザー",
            picture="",
            plan="standard",
            usage_count=12,
        )
        db.session.add(user)
        db.session.commit()
    session["user_id"] = user.id
    # 開発用: app_user セッションも設定する
    owner = AppUser.query.filter_by(username='owner').first()
    if owner:
        session['app_user_id'] = owner.id
        session['app_user_role'] = owner.role
        session['app_username'] = owner.username
    return redirect(url_for("executive_dashboard"))


@app.route("/dashboard")
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    history = AiHistory.query.filter_by(user_id=user.id).order_by(AiHistory.created_at.desc()).limit(5).all()
    return render_template("dashboard.html", user=user, history=history)


# ── 既存AI機能 ────────────────────────────────────────────

@app.route("/description", methods=["GET", "POST"])
def description():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    result = None
    if request.method == "POST":
        data = request.form
        prompt = f"""あなたは不動産会社のプロのコピーライターです。
以下の物件情報をもとに、購買意欲を高める魅力的な物件紹介文を作成してください。

【物件情報】
- 種別: {data.get('type', '')}
- 所在地: {data.get('location', '')}
- 築年数: {data.get('age', '')}年
- 間取り: {data.get('layout', '')}
- 専有面積: {data.get('area', '')}㎡
- 価格: {data.get('price', '')}万円
- 特徴・設備: {data.get('features', '')}

【出力形式】
- キャッチコピー（1文）
- 物件紹介文（200〜300文字）
- おすすめポイント（箇条書き3点）

読者は住宅購入を検討している一般の方です。専門用語は避け、温かみのある文章でお願いします。"""

        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = message.content[0].text
        history = AiHistory(user_id=user.id, feature="description", input_data=str(dict(data)), output_data=result)
        db.session.add(history)
        user.usage_count += 1
        db.session.commit()

    return render_template("description.html", user=user, result=result)


@app.route("/inquiry", methods=["GET", "POST"])
def inquiry():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    result = None
    if request.method == "POST":
        data = request.form
        prompt = f"""あなたは不動産会社の丁寧なカスタマーサポート担当者です。
以下のお客様からの問い合わせに対して、プロフェッショナルな返信メールを作成してください。

【会社名】{data.get('company', '株式会社〇〇不動産')}
【担当者名】{data.get('staff', '担当者')}
【お客様のお問い合わせ内容】
{data.get('inquiry', '')}

【返信のポイント】
- 丁寧で親切な文体
- お客様の不安や疑問に寄り添う
- 次のアクション（内見予約・電話相談など）を自然に促す
- 署名を含める

件名から本文まで完全なメール形式で作成してください。"""

        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = message.content[0].text
        history = AiHistory(user_id=user.id, feature="inquiry", input_data=str(dict(data)), output_data=result)
        db.session.add(history)
        user.usage_count += 1
        db.session.commit()

    return render_template("inquiry.html", user=user, result=result)


@app.route("/assessment", methods=["GET", "POST"])
def assessment():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    result = None
    if request.method == "POST":
        data = request.form
        prompt = f"""あなたは不動産価格査定の専門家です。
以下の物件情報をもとに、市場価格の査定レポートを作成してください。

【物件情報】
- 種別: {data.get('type', '')}
- 所在地（市区町村まで）: {data.get('location', '')}
- 最寄り駅・徒歩分数: {data.get('station', '')}
- 築年数: {data.get('age', '')}年
- 間取り: {data.get('layout', '')}
- 専有面積 / 土地面積: {data.get('area', '')}㎡
- 建物構造: {data.get('structure', '')}
- リフォーム歴: {data.get('reform', '')}
- 売主希望価格: {data.get('hope_price', '')}万円（参考）

【出力形式】
1. 査定価格レンジ（例：3,500万〜3,800万円）
2. 査定根拠（立地・築年数・相場観などを説明）
3. 価格アップのアドバイス（2〜3点）
4. 売却戦略の提案

※実際の査定はあくまで参考値です。正確な査定には現地調査が必要です。"""

        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        result = message.content[0].text
        history = AiHistory(user_id=user.id, feature="assessment", input_data=str(dict(data)), output_data=result)
        db.session.add(history)
        user.usage_count += 1
        db.session.commit()

    return render_template("assessment.html", user=user, result=result)


# ── 幹部向け管理ツール：ページルート ─────────────────────

@app.route("/executive")
@login_required
def executive_dashboard():
    """売上管理ダッシュボード"""
    staff_list = Staff.query.filter_by(is_active=True).all()
    year, month = current_ym()
    return render_template("executive_dashboard.html",
                           staff_list=staff_list, year=year, month=month,
                           now=datetime.now())


@app.route("/sales")
@login_required
def sales_management():
    """営業管理ページ：KPI入力・閲覧"""
    stores = Store.query.filter_by(is_active=True).all()
    staff_list = Staff.query.filter_by(is_active=True).all()
    year, month = current_ym()
    return render_template("sales_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now())


@app.route("/leads")
@login_required
def leads_management():
    """反響管理ページ：リード一覧・追加"""
    stores = Store.query.filter_by(is_active=True).all()
    staff_list = Staff.query.filter_by(is_active=True).all()
    # 最新100件を表示
    leads = (Lead.query
             .order_by(Lead.received_at.desc())
             .limit(100)
             .all())
    year, month = current_ym()
    return render_template("leads_management.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now())


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
    lead = Lead(
        source=data.get('source') or data.get('media', ''),
        received_at=datetime.now(),
        status=data.get('status', '未対応'),
        assigned_staff_id=data.get('assigned_staff_id') or data.get('assignee_id') or None,
        store_id=data.get('store_id') or 1,
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
def accounting():
    """会計・PL管理ページ"""
    stores = Store.query.filter_by(is_active=True).all()
    year, month = current_ym()
    return render_template("accounting.html", stores=stores, year=year, month=month,
                           now=datetime.now())


@app.route("/staff-ranking")
@login_required
def staff_ranking():
    """スタッフランキングページ"""
    stores = Store.query.filter_by(is_active=True).all()
    staff_list = Staff.query.filter_by(is_active=True).all()
    year, month = current_ym()
    return render_template("staff_ranking.html", stores=stores, staff_list=staff_list,
                           year=year, month=month, now=datetime.now())


# ── 幹部向け管理ツール：データAPI（JSON） ────────────────

@app.route("/api/kpi/summary")
def api_kpi_summary():
    """KPIサマリを返す（year/monthパラメータで任意月指定可）"""
    cy, cm = current_ym()
    year  = request.args.get('year',  type=int) or cy
    month = request.args.get('month', type=int) or cm

    def get_kpis(y, m):
        return SalesKPI.query.filter_by(year=y, month=m).all()

    def get_pl(y, m):
        return PLRecord.query.filter_by(year=y, month=m).all()

    # 今月・前月
    kpis_now  = get_kpis(year, month)
    prev_m    = month - 1 if month > 1 else 12
    prev_y    = year if month > 1 else year - 1
    kpis_prev = get_kpis(prev_y, prev_m)
    pls_now   = get_pl(year, month)
    pls_prev  = get_pl(prev_y, prev_m)

    sales_now  = sum(k.sales_amount for k in kpis_now)
    sales_prev = sum(k.sales_amount for k in kpis_prev)
    contracts_now  = sum(k.contracts for k in kpis_now)
    contracts_prev = sum(k.contracts for k in kpis_prev)

    # 今月PL
    rev_now    = sum(p.revenue for p in pls_now)
    gp_now     = sum(p.gross_profit for p in pls_now)
    ad_now     = sum(p.ad_cost for p in pls_now)
    labor_now  = sum(p.labor_cost for p in pls_now)
    fixed_now  = sum(p.other_fixed for p in pls_now)
    var_now    = sum(p.other_variable for p in pls_now)
    profit_now = gp_now - ad_now - labor_now - fixed_now - var_now

    rev_prev   = sum(p.revenue for p in pls_prev)
    gp_prev    = sum(p.gross_profit for p in pls_prev)
    ad_prev    = sum(p.ad_cost for p in pls_prev)
    labor_prev = sum(p.labor_cost for p in pls_prev)
    fixed_prev = sum(p.other_fixed for p in pls_prev)
    var_prev   = sum(p.other_variable for p in pls_prev)
    profit_prev = gp_prev - ad_prev - labor_prev - fixed_prev - var_prev

    # 広告ROI（今月）
    roi_now  = round(rev_now  / ad_now  * 100, 1) if ad_now  > 0 else 0
    roi_prev = round(rev_prev / ad_prev * 100, 1) if ad_prev > 0 else 0

    # 未対応反響
    unhandled = Lead.query.filter_by(status='未対応').count()

    # 着地予測（今月日割り）
    today_day = date.today().day
    days_in_month = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 28
    forecast = round(sales_now / today_day * days_in_month) if today_day > 0 else sales_now

    # 前年同月
    kpis_yoy  = get_kpis(year - 1, month)
    pls_yoy   = get_pl(year - 1, month)
    sales_yoy     = sum(k.sales_amount for k in kpis_yoy)
    contracts_yoy = sum(k.contracts for k in kpis_yoy)
    rev_yoy   = sum(p.revenue for p in pls_yoy)
    gp_yoy    = sum(p.gross_profit for p in pls_yoy)
    ad_yoy    = sum(p.ad_cost for p in pls_yoy)
    labor_yoy = sum(p.labor_cost for p in pls_yoy)
    fixed_yoy = sum(p.other_fixed for p in pls_yoy)
    var_yoy   = sum(p.other_variable for p in pls_yoy)
    profit_yoy = gp_yoy - ad_yoy - labor_yoy - fixed_yoy - var_yoy
    roi_yoy    = round(rev_yoy / ad_yoy * 100, 1) if ad_yoy > 0 else 0

    def diff_pct(now, prev):
        if prev == 0:
            return 0
        return round((now - prev) / prev * 100, 1)

    # 反響管理データがあればそちらを優先（自動連携）
    lead_stats_now  = LeadMediaStat.query.filter_by(store_id=1, year=year, month=month).all()
    lead_stats_prev = LeadMediaStat.query.filter_by(store_id=1, year=prev_y, month=prev_m).all()
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
    """過去12ヶ月のKPIデータをグラフ用に返す"""
    store_id = request.args.get('store_id', type=int)
    months_data = []

    today = date.today()
    for i in range(11, -1, -1):
        target = today.replace(day=1) - timedelta(days=1)
        for _ in range(i):
            target = target.replace(day=1) - timedelta(days=1)
        y, m = target.year, target.month

        query = SalesKPI.query.filter_by(year=y, month=m)
        if store_id:
            query = query.filter_by(store_id=store_id)
        kpis = query.all()

        pls = PLRecord.query.filter_by(year=y, month=m)
        if store_id:
            pls = pls.filter_by(store_id=store_id)
        pls = pls.all()
        months_data.append({
            'label':       f'{y}/{m:02d}',
            'year':        y,
            'month':       m,
            'inquiries':   sum(k.inquiries for k in kpis),
            'contracts':   sum(k.contracts for k in kpis),
            'sales':       sum(k.sales_amount for k in kpis),
            'gross_profit':sum(p.gross_profit for p in pls),
            'ad_cost':     sum(p.ad_cost for p in pls),
        })

    return jsonify(months_data)


@app.route("/api/kpi/staff")
def api_kpi_staff():
    """スタッフ別KPIデータを返す"""
    year  = request.args.get('year',  type=int) or current_ym()[0]
    month = request.args.get('month', type=int) or current_ym()[1]
    store_id = request.args.get('store_id', type=int)

    query = SalesKPI.query.filter_by(year=year, month=month)
    if store_id:
        query = query.filter_by(store_id=store_id)
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
            'sales_amount':kpi.sales_amount,
            'option_sales':kpi.option_sales,
        })

    # 売上降順でソート
    result.sort(key=lambda x: x['sales_amount'], reverse=True)
    return jsonify(result)


@app.route("/api/leads/summary")
def api_leads_summary():
    """反響サマリを返す（媒体別・ステータス別）"""
    store_id = request.args.get('store_id', type=int)
    year     = request.args.get('year',  type=int)
    month    = request.args.get('month', type=int)

    query = Lead.query
    if store_id:
        query = query.filter_by(store_id=store_id)
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
def api_leads_monthly_stats():
    """媒体別月次反響統計を返す"""
    cy, cm = current_ym()
    year  = request.args.get('year',  type=int) or cy
    month = request.args.get('month', type=int) or cm
    store_id = request.args.get('store_id', type=int) or 1

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


@app.route("/api/leads/monthly-stats", methods=["POST"])
def api_leads_monthly_stats_input():
    """媒体別月次反響統計を手動入力"""
    data = request.get_json() or request.form
    year  = int(data.get('year', current_ym()[0]))
    month = int(data.get('month', current_ym()[1]))
    store_id = int(data.get('store_id', 1))
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
    return jsonify({'status': 'ok', 'id': stat.id})


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
    store_id = int(request.form.get('store_id', 1))

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


@app.route("/api/pl/summary")
def api_pl_summary():
    """PLサマリを返す（店舗別・月次）"""
    store_id = request.args.get('store_id', type=int)
    year     = request.args.get('year',  type=int) or current_ym()[0]
    month    = request.args.get('month', type=int) or current_ym()[1]

    query = PLRecord.query.filter_by(year=year, month=month)
    if store_id:
        query = query.filter_by(store_id=store_id)
    pls = query.all()

    result = []
    for pl in pls:
        store = Store.query.get(pl.store_id)
        operating_profit = (pl.gross_profit
                            - pl.ad_cost
                            - pl.labor_cost
                            - pl.other_fixed
                            - pl.other_variable)
        result.append({
            'pl_id':          pl.id,
            'store_id':       pl.store_id,
            'store_name':     store.name if store else '不明',
            'revenue':        pl.revenue,
            'gross_profit':   pl.gross_profit,
            'gross_margin':   round(pl.gross_profit / pl.revenue * 100, 1) if pl.revenue else 0,
            'ad_cost':        pl.ad_cost,
            'labor_cost':     pl.labor_cost,
            'other_fixed':    pl.other_fixed,
            'other_variable': pl.other_variable,
            'operating_profit': operating_profit,
            'op_margin':      round(operating_profit / pl.revenue * 100, 1) if pl.revenue else 0,
            # 収入詳細
            'brokerage_fee':       pl.brokerage_fee or 0,
            'ad_income':           pl.ad_income or 0,
            'lifeline_income':     pl.lifeline_income or 0,
            'moving_income':       pl.moving_income or 0,
            'fire_insurance_income': pl.fire_insurance_income or 0,
            'other_income':        pl.other_income or 0,
            # 広告費詳細
            'suumo_cost':     pl.suumo_cost or 0,
            'homes_cost':     pl.homes_cost or 0,
            'athome_cost':    pl.athome_cost or 0,
            'instagram_cost': pl.instagram_cost or 0,
            'tiktok_cost':    pl.tiktok_cost or 0,
            'google_ads_cost':pl.google_ads_cost or 0,
            'line_cost':      pl.line_cost or 0,
            'hp_cost':        pl.hp_cost or 0,
            'meo_cost':       pl.meo_cost or 0,
            'other_ad_cost':  pl.other_ad_cost or 0,
            # 人件費詳細
            'regular_salary':  pl.regular_salary or 0,
            'parttime_salary': pl.parttime_salary or 0,
            'commission_pay':  pl.commission_pay or 0,
            # 固定費詳細
            'pl_rent':       pl.pl_rent or 0,
            'pl_parking':    pl.pl_parking or 0,
            'pl_copier':     pl.pl_copier or 0,
            'pl_internet':   pl.pl_internet or 0,
            'pl_consultant': pl.pl_consultant or 0,
            'pl_insurance':  pl.pl_insurance or 0,
            'pl_cloud':      pl.pl_cloud or 0,
        })

    # カスタム費用項目の値を type 別に追加
    for r in result:
        custom_vals = PLCustomValue.query.filter_by(
            store_id=r['store_id'], year=year, month=month
        ).all()
        r['fixed_items']    = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '固定費']
        r['variable_items'] = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') == '変動費']
        r['custom_items']   = [{'name': v.item_name, 'amount': v.amount} for v in custom_vals if (v.item_type or '固定費') not in ('固定費','変動費')]

    # テンプレート一覧（type別）
    all_items = PLCustomItem.query.filter_by(store_id=1).order_by(PLCustomItem.sort_order).all()
    template_fixed    = [i.name for i in all_items if (i.item_type or '固定費') == '固定費']
    template_variable = [i.name for i in all_items if (i.item_type or '固定費') == '変動費']
    template_items    = [i.name for i in all_items if (i.item_type or '固定費') not in ('固定費','変動費')]

    return jsonify({
        'year': year, 'month': month, 'stores': result,
        'template_fixed': template_fixed,
        'template_variable': template_variable,
        'template_items': template_items,
    })


@app.route("/api/pl/custom-items")
def api_pl_custom_items():
    """PLカスタム項目テンプレート一覧（type別）"""
    items = PLCustomItem.query.filter_by(store_id=1).order_by(PLCustomItem.sort_order).all()
    return jsonify({
        'fixed':    [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') == '固定費'],
        'variable': [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') == '変動費'],
        'other':    [{'id': i.id, 'name': i.name} for i in items if (i.item_type or '固定費') not in ('固定費','変動費')],
    })


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
def api_kpi_input():
    """KPIデータを入力・更新する"""
    data = request.get_json() or request.form
    staff_id = int(data.get('staff_id', 0))
    store_id = int(data.get('store_id', 0))
    year     = int(data.get('year', current_ym()[0]))
    month    = int(data.get('month', current_ym()[1]))

    # 既存レコードがあれば更新、なければ新規作成
    kpi = SalesKPI.query.filter_by(staff_id=staff_id, store_id=store_id,
                                    year=year, month=month).first()
    if not kpi:
        kpi = SalesKPI(staff_id=staff_id, store_id=store_id, year=year, month=month)
        db.session.add(kpi)

    kpi.inquiries    = int(data.get('inquiries', kpi.inquiries))
    kpi.store_visits = int(data.get('store_visits', kpi.store_visits))
    kpi.viewings     = int(data.get('viewings', kpi.viewings))
    kpi.applications = int(data.get('applications', kpi.applications))
    kpi.contracts    = int(data.get('contracts', kpi.contracts))
    kpi.cancellations= int(data.get('cancellations', kpi.cancellations))
    kpi.sales_amount = float(data.get('sales_amount', kpi.sales_amount))
    kpi.option_sales = float(data.get('option_sales', kpi.option_sales))
    db.session.commit()

    return jsonify({'status': 'ok', 'id': kpi.id})


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


@app.route("/api/pl/input", methods=["POST"])
def api_pl_input():
    """PLデータを入力・更新する"""
    data = request.get_json() or request.form
    store_id = int(data.get('store_id', 0) or 1)
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
        if not raw: return
        if isinstance(raw, str):
            import json as _json
            try: raw = _json.loads(raw)
            except: return
        for item in raw:
            name   = str(item.get('name', '')).strip()
            amount = float(item.get('amount', 0) or 0)
            if not name: continue
            cv = PLCustomValue.query.filter_by(store_id=store_id, year=year, month=month, item_name=name, item_type=item_type).first()
            if not cv:
                cv = PLCustomValue(store_id=store_id, year=year, month=month, item_name=name, item_type=item_type)
                db.session.add(cv)
            cv.amount = amount
            # テンプレート登録
            existing = PLCustomItem.query.filter_by(store_id=store_id, name=name, item_type=item_type).first()
            if not existing:
                max_order = db.session.query(db.func.max(PLCustomItem.sort_order)).filter_by(store_id=store_id).scalar() or 0
                db.session.add(PLCustomItem(store_id=store_id, name=name, item_type=item_type, sort_order=max_order + 1))

    _save_typed_items('fixed_items', '固定費')
    _save_typed_items('variable_items', '変動費')
    _save_typed_items('custom_items', 'その他')
    db.session.commit()

    return jsonify({'status': 'ok', 'id': pl.id})


@app.route("/api/ad/input", methods=["POST"])
def api_ad_input():
    """広告費を入力・更新する"""
    data = request.get_json() or request.form
    store_id = int(data.get('store_id', 0))
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
def api_store_add():
    """店舗を追加する"""
    data = request.get_json() or request.form
    store = Store(
        name=data.get('name', ''),
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
    """スタッフ一覧（KPIデータに関係なく全スタッフを返す）"""
    staff_list = Staff.query.filter_by(is_active=True).all()
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
    store_id = request.form.get('store_id', type=int) or 1

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
    staff_list = Staff.query.filter_by(is_active=True).all()
    # アカウント一覧はオーナーのみ表示
    user = AppUser.query.get(session['app_user_id'])
    is_owner = user and user.role == 'owner'
    accounts = AppUser.query.filter_by(is_active=True).all() if is_owner else []
    return render_template("settings.html",
                           staff_list=staff_list, accounts=accounts,
                           is_owner=is_owner,
                           now=datetime.now())


@app.route("/api/settings/staff/add", methods=["POST"])
@login_required
def api_settings_staff_add():
    """スタッフ追加"""
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
        store_id=1,
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
@owner_required
def api_settings_account_add():
    """アカウント追加"""
    data = request.get_json() or request.form
    if AppUser.query.filter_by(username=data.get('username', ''), is_active=True).first():
        return jsonify({'status': 'error', 'message': 'そのユーザー名は既に使用されています'}), 400
    user = AppUser(
        username=data.get('username', ''),
        password_hash=generate_password_hash(data.get('password', '')),
        role=data.get('role', 'staff'),
        staff_id=int(data.get('staff_id')) if data.get('staff_id') else None,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': user.id})


@app.route("/api/settings/account/<int:account_id>/password", methods=["PUT"])
@owner_required
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
@owner_required
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    host = "0.0.0.0" if _IS_POSTGRES else "127.0.0.1"
    app.run(debug=False, port=port, host=host, use_reloader=False)
