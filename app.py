import os
import sys  # 核心追加：引入系统底层库以支持 PyInstaller 打包路由
import base64
import json
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from openai import OpenAI

# ==================== Advanced Render & PyInstaller Path Engine ====================
if os.environ.get('RENDER'):
    app = Flask(__name__)
    
    # ⚡【智能路径自适应防崩溃引擎】
    # 自动检测 /data 文件夹是否存在且允许程序写入（判断是否成功挂载了 Render 持久化云盘）
    if os.path.exists('/data') and os.access('/data', os.W_OK):
        base_dir = '/data'  # 付费版：使用不丢失数据的持久化网盘
        print("[Render Environment] Bound to Persistent Disk Storage (/data)")
    else:
        base_dir = os.path.abspath(os.path.dirname(__file__))  # 免费版：降级到当前项目安全的根目录运行
        print("[Render Environment] Disk not found or unauthorized. Fallback to Ephemeral App Root.")
        
elif getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
    base_dir = os.path.dirname(sys.executable)
else:
    app = Flask(__name__)
    base_dir = os.path.abspath(os.path.dirname(__file__))

app.config['SECRET_KEY'] = 'local_network_secret_key_12345'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制上传发票图片最大 16MB

# ==================== 外置共享网盘/本地路径兼容注入 ====================
shared_target_dir = base_dir 

config_file_path = os.path.join(base_dir, 'config.json')
if os.path.exists(config_file_path):
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            if config_data.get('shared_network_path'):
                shared_target_dir = config_data['shared_network_path'].strip()
                print(f"[Network Redirect Active] Data targeted to: {shared_target_dir}")
    except Exception as e:
        print(f"[Config Error] Failed to parse config.json: {e}")

# 配置动态绝对路径
app.config['UPLOAD_FOLDER'] = os.path.abspath(os.path.join(shared_target_dir, 'uploads'))
db_absolute_path = os.path.abspath(os.path.join(shared_target_dir, 'finance.db'))

# 针对 Windows 网络 UNC 路径进行 SQLite 连接串规范化洗礼
normalized_db_path = db_absolute_path.replace('\\', '/')
if normalized_db_path.startswith('//') or normalized_db_path.startswith('\\\\'):
    if not normalized_db_path.startswith('///'):
        normalized_db_path = '/' + normalized_db_path.lstrip('/')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{normalized_db_path}"

# 建立 15 秒并发原子写入排队机制
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'timeout': 15}
}

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ==================== OpenAI / Vision LLM 智能配置 ====================
# 配置你的 AI 服务商密钥与基准 URL（如使用国内中转或本地大模型，修改 base_url 即可）
ai_client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "your-api-key-here"),
    base_url="https://api.openai.com/v1" 
)

# ==================== Database Models ====================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False) # 'master', 'accountant', 'member'

class AccountType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False) # e.g., Cash, Bank Account

class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)    # 'Snack Station' 或 'PASN Event'
    type = db.Column(db.String(20), nullable=False)        # 'Reimbursement', 'Income', 'Direct Expense'
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(40), default='Approved')   
    submitter_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    approver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    invoice_file = db.Column(db.String(200), nullable=True) # 存储合规物理相对路径
    account_type_id = db.Column(db.Integer, db.ForeignKey('account_type.id'), nullable=True)
    bank_submitter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    submitter = db.relationship('User', foreign_keys=[submitter_id])
    approver = db.relationship('User', foreign_keys=[approver_id])
    bank_submitter = db.relationship('User', foreign_keys=[bank_submitter_id])
    account_type = db.relationship('AccountType', foreign_keys=[account_type_id])

# ⚡ 新增审计数据模型：工作流全生命周期操作过程记录表
class WorkflowLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, nullable=True)         # 关联单据ID（允许为空，以备单据被物理擦除时保留完整外部审计链）
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # 执行本步骤的操作员
    action = db.Column(db.String(50), nullable=False)        # 财务具体操作动作
    stage_before = db.Column(db.String(50), nullable=True)   # 操作前业务所处阶段状态
    stage_after = db.Column(db.String(50), nullable=True)    # 操作后业务跃迁目标状态
    timestamp = db.Column(db.DateTime, default=datetime.utcnow) # 操作时间戳
    details = db.Column(db.Text, nullable=True)               # 详细操作过程日志细化描述

    user = db.relationship('User', foreign_keys=[user_id])

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==================== AI 智能识别 & 树状归档组件 ====================
def ai_analyze_invoice_vision(file_path):
    """
    通过 Vision LLM 智能提取发票图片总金额与产品信息明细摘要
    """
    try:
        with open(file_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode('utf-8')
        ext = os.path.splitext(file_path)[1].lower()
        mime_type = "image/jpeg" if ext in ['.jpg', '.jpeg'] else "image/png"
    except Exception as e:
        print(f"[AI OCR Error] Cannot read archive file: {e}")
        return 0.0, "AI Error: File unreadable"

    prompt = """
    You are an expert accountant. Analyze this invoice/receipt image carefully.
    Extract the following information and return it STRICTLY in a raw JSON format (do not wrap in markdown ```json blocks):
    {
        "total_amount": float, 
        "items_summary": "string" 
    }
    Make your best educated guess for total_amount if the text is blurry.
    """
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o", 
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.0
        )
        result_text = response.choices[0].message.content.strip()
        data = json.loads(result_text)
        return float(data.get("total_amount", 0.0)), data.get("items_summary", "Analyzed by AI.")
    except Exception as e:
        print(f"[AI OCR Interface Error]: {e}")
        return 0.0, "AI Connection timeout. Please enter description manually."

def save_and_archive_invoice(uploaded_file, category_name, username, amount):
    """
    按 活动项目/年份-月份 动态创建本地物理目录并对数字化发票进行规范化重命名归档
    """
    if not uploaded_file or uploaded_file.filename == '':
        return None
    safe_category = secure_filename(category_name.replace(" ", "_"))
    current_month_dir = datetime.now().strftime("%Y-%m")
    
    archive_dir = os.path.join(app.config['UPLOAD_FOLDER'], safe_category, current_month_dir)
    if not os.path.exists(archive_dir):
        os.makedirs(archive_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_origin_name = secure_filename(uploaded_file.filename)
    new_filename = f"{timestamp}_{secure_filename(username)}_{amount:.2f}_{safe_origin_name}"
    
    full_save_path = os.path.join(archive_dir, new_filename)
    uploaded_file.seek(0) 
    uploaded_file.save(full_save_path)
    
    return os.path.join(safe_category, current_month_dir, new_filename).replace("\\", "/")

# ==================== Core Authorization Routes ====================
@app.route('/')
@login_required
def index():
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ==================== Dashboard Logic ====================
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        category = request.form.get('category')
        rec_type = request.form.get('type')
        description = request.form.get('description')
        acc_type_id = request.form.get('account_type_id') 
        form_amount_str = request.form.get('amount')
        
        file = request.files.get('invoice')
        relative_invoice_path = None
        amount = 0.0
        
        if file and file.filename != '':
            temp_filename = "temp_" + secure_filename(file.filename)
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file.save(temp_path)
            
            ai_amount, ai_products = ai_analyze_invoice_vision(temp_path)
            amount = float(form_amount_str) if form_amount_str else ai_amount
            if not description or description.strip() == '':
                description = f"[AI Auto] {ai_products}"
                
            relative_invoice_path = save_and_archive_invoice(file, category, current_user.username, amount)
            
            if os.path.exists(temp_path):
                os.remove(temp_path)
        else:
            amount = float(form_amount_str) if form_amount_str else 0.0

        new_record = Record(
            category=category,
            type=rec_type,
            amount=amount,
            description=description,
            submitter_id=current_user.id,
            invoice_file=relative_invoice_path,
            account_type_id=int(acc_type_id) if acc_type_id else None
        )
        
        if rec_type == 'Reimbursement': 
            new_record.status = 'Pending Request' 
        else:
            if current_user.role not in ['master', 'accountant']:
                flash('Permission Denied! Only Finance personnel can log Direct Income/Expense.', 'danger')
                return redirect(url_for('dashboard'))
            new_record.status = 'Approved' 
            new_record.approver_id = current_user.id
            
        db.session.add(new_record)
        db.session.commit()

        # ⚡ 埋点日志追踪：记录新发起的报销或直接账目入账行为 (Stage 1)
        action_title = "Request Initialized" if rec_type == 'Reimbursement' else "Direct Record Created"
        log = WorkflowLog(
            record_id=new_record.id, user_id=current_user.id, action=action_title,
            stage_before="None", stage_after=new_record.status,
            details=f"Scope: {category} | Type: {rec_type} | Sum Total: ${amount:.2f}"
        )
        db.session.add(log)
        db.session.commit()

        flash('Record filed and submitted successfully into workflow!', 'success')
        return redirect(url_for('dashboard'))

    # 动态资产盘点
    all_approved = Record.query.filter(Record.status.in_(['Approved', 'Approved (Online)', 'Approved (Cash)'])).all()
    stats = {'snack_income': 0.0, 'snack_expense': 0.0, 'pasn_income': 0.0, 'pasn_expense': 0.0}
    for r in all_approved:
        if r.category == 'Snack Station':
            if r.type == 'Income': stats['snack_income'] += r.amount
            else: stats['snack_expense'] += r.amount
        elif r.category == 'PASN Event':
            if r.type == 'Income': stats['pasn_income'] += r.amount
            else: stats['pasn_expense'] += r.amount
                
    stats['snack_balance'] = stats['snack_income'] - stats['snack_expense']
    stats['pasn_balance'] = stats['pasn_income'] - stats['pasn_expense']

    if current_user.role in ['master', 'accountant']:
        records = Record.query.order_by(Record.date.desc()).all()
    else:
        records = Record.query.filter_by(submitter_id=current_user.id).order_by(Record.date.desc()).all()
        
    return render_template('dashboard.html', records=records, stats=stats, account_types=AccountType.query.all())

# ==================== Advanced Workflow Logic (Stage 2 & 3) ====================
@app.route('/record/submit_bank/<int:record_id>')
@login_required
def submit_bank(record_id):
    """ Stage 2: 财务人员网银转账支付数据录入提交 """
    if current_user.role not in ['master', 'accountant']:
        flash('Access Denied!', 'danger')
        return redirect(url_for('dashboard'))
        
    record = Record.query.get_or_404(record_id)
    if record.status != 'Pending Request':
        flash('Invalid transaction stage conversion.', 'warning')
        return redirect(url_for('dashboard'))
        
    old_status = record.status
    record.status = 'Pending Authorization'
    record.bank_submitter_id = current_user.id  # 锁定网银操作录入员
    db.session.commit()

    # ⚡ 埋点日志追踪：记录财务审查并录入手机网上银行步骤完成 (Stage 2)
    log = WorkflowLog(
        record_id=record.id, user_id=current_user.id, action="Bank Transfer Submitted",
        stage_before=old_status, stage_after=record.status,
        details=f"Accountant registered internet banking clearance entry line item."
    )
    db.session.add(log)
    db.session.commit()

    flash('Internet banking transfer registered. Status advanced to Awaiting Authorization.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/record/authorize/<int:record_id>/<string:method>')
@login_required
def authorize_payment(record_id, method):
    """ Stage 3: 双人复核职责隔离，另一个同侪进行最终授权放款 """
    if current_user.role not in ['master', 'accountant']:
        flash('Access Denied!', 'danger')
        return redirect(url_for('dashboard'))
        
    record = Record.query.get_or_404(record_id)
    if record.status != 'Pending Authorization':
        flash('This transaction is not awaiting authorization settlement.', 'warning')
        return redirect(url_for('dashboard'))
        
    # 职责分离内审安全验证拦截
    if record.submitter_id == current_user.id:
        flash('Audit Conflict Blocked: You cannot authorize your own requested bills.', 'danger')
        return redirect(url_for('dashboard'))
    if record.bank_submitter_id == current_user.id and current_user.role == 'accountant':
        flash('Audit Conflict Blocked: Segregation of Duties! Peer reviews are mandatory.', 'danger')
        return redirect(url_for('dashboard'))
        
    old_status = record.status
    if method == 'online':
        record.status = 'Approved (Online)'
    elif method == 'cash':
        record.status = 'Approved (Cash)'
    else:
        return redirect(url_for('dashboard'))
        
    record.approver_id = current_user.id
    db.session.commit()

    # ⚡ 埋点日志追踪：记录同侪放款核准，全工作流闭环 (Stage 3)
    log = WorkflowLog(
        record_id=record.id, user_id=current_user.id, action=f"Authorized Via {method.upper()}",
        stage_before=old_status, stage_after=record.status,
        details=f"Final authorization verified. Clearance cleared via payment method [{method.upper()}]."
    )
    db.session.add(log)
    db.session.commit()

    flash(f'Disbursement authorized via [{method.upper()}]. Workflow sequence closed.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/record/reject_workflow/<int:record_id>')
@login_required
def reject_workflow(record_id):
    """ 全节点流程通用驳回拒绝路由 """
    if current_user.role not in ['master', 'accountant']: 
        return redirect(url_for('dashboard'))
    record = Record.query.get_or_404(record_id)
    if record.submitter_id == current_user.id:
        flash('Audit Conflict: Self-auditing is forbidden.', 'danger')
        return redirect(url_for('dashboard'))
        
    old_status = record.status
    record.status = 'Rejected'
    record.approver_id = current_user.id
    db.session.commit()

    # ⚡ 埋点日志追踪：记录财务一键废弃拒绝申请的操作过程
    log = WorkflowLog(
        record_id=record.id, user_id=current_user.id, action="Workflow Rejected",
        stage_before=old_status, stage_after=record.status,
        details=f"Operational process declined and flagged as invalid by {current_user.username}."
    )
    db.session.add(log)
    db.session.commit()

    flash('Financial workflow declined and locked.', 'info')
    return redirect(url_for('dashboard'))

# ==================== NEW FEATURE: Core Operational Process Logs Panel ====================
@app.route('/logs')
@login_required
def logs_panel():
    """ 
    系统全流程步骤追踪专属控制台（仅内审及主管、财务可查阅）
    """
    if current_user.role not in ['master', 'accountant']:
        flash('Access Denied: Log inspection console is restricted.', 'danger')
        return redirect(url_for('dashboard'))
        
    # 按照发生顺序逆序（最新最先）调取底层历史行为镜像
    all_logs = WorkflowLog.query.order_by(WorkflowLog.timestamp.desc()).all()
    return render_template('logs.html', logs=all_logs)

# ==================== Core Audit Search Portal ====================
@app.route('/audit')
@login_required
def audit_panel():
    """ 专属高级审计对账中心：支持按精确日期和内容关键字检索数字化发票 """
    if current_user.role not in ['master', 'accountant']:
        flash('Access Denied: Audit console is restricted to internal finance roles.', 'danger')
        return redirect(url_for('dashboard'))
        
    search_keyword = request.args.get('keyword', '').strip()
    search_date_str = request.args.get('date', '').strip()
    
    query = Record.query
    
    if search_keyword:
        query = query.filter(Record.description.ilike(f"%{search_keyword}%"))
        
    if search_date_str:
        try:
            target_date = datetime.strptime(search_date_str, '%Y-%m-%d').date()
            query = query.filter(db.cast(Record.date, db.Date) == target_date)
        except ValueError:
            flash('Invalid date syntax encountered.', 'warning')
            
    records = query.order_by(Record.date.desc()).all()
    return render_template('audit.html', records=records, keyword=search_keyword, search_date=search_date_str)

# 采用 path 转换器支持安全且无视层级的发票资源跨目录浏览下载
@app.route('/uploads/<path:filename>')
@login_required
def view_invoice(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==================== Edit & Delete Tracking ====================
@app.route('/record/delete/<int:record_id>')
@login_required
def delete_record(record_id):
    record = Record.query.get_or_404(record_id)
    if (record.submitter_id == current_user.id and record.status == 'Pending Request') or current_user.role in ['master', 'accountant']:
        
        # ⚡ 埋点日志追踪：在数据记录行被物理抹除前，强制将关键凭证留底备份到只读日志表
        log = WorkflowLog(
            record_id=record.id, user_id=current_user.id, action="Record Deleted",
            stage_before=record.status, stage_after="Erased/Purged",
            details=f"Permanently purged entry line item. Category: {record.category} | Amount: ${record.amount:.2f} | Requester ID: {record.submitter_id}"
        )
        db.session.add(log)
        
        db.session.delete(record)
        db.session.commit()
        flash('Financial record has been successfully purged.', 'success')
    else:
        flash('Unauthorized command or locked state.', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/record/edit/<int:record_id>', methods=['GET', 'POST'])
@login_required
def edit_record(record_id):
    record = Record.query.get_or_404(record_id)
    if not ((record.submitter_id == current_user.id and record.status == 'Pending Request') or current_user.role in ['master', 'accountant']):
        flash('Permission Denied! Locked records cannot be modified.', 'danger')
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        old_amount = record.amount
        record.category = request.form.get('category')
        record.amount = float(request.form.get('amount'))
        record.description = request.form.get('description')
        record.account_type_id = int(request.form.get('account_type_id'))
        
        old_status = record.status
        if current_user.role not in ['master', 'accountant']:
            record.status = 'Pending Request'
            record.approver_id = None
            record.bank_submitter_id = None
            
        file = request.files.get('invoice')
        if file and file.filename != '':
            record.invoice_file = save_and_archive_invoice(file, record.category, current_user.username, record.amount)
            
        db.session.commit()

        # ⚡ 埋点日志追踪：记录账目遭到修改的过程
        log = WorkflowLog(
            record_id=record.id, user_id=current_user.id, action="Record Amended",
            stage_before=old_status, stage_after=record.status,
            details=f"Modified entry fields. Adjusted Amount deviation: ${old_amount:.2f} -> ${record.amount:.2f}."
        )
        db.session.add(log)
        db.session.commit()

        flash('Record changes saved successfully.', 'success')
        return redirect(url_for('dashboard'))
        
    return render_template('edit_record.html', record=record, account_types=AccountType.query.all())

# ==================== Custom Data Export ====================
@app.route('/export', methods=['GET', 'POST'])
@login_required
def export_data():
    if current_user.role not in ['master', 'accountant']:
        flash('Unauthorized to export financial metrics.', 'danger')
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        selected_fields = request.form.getlist('fields')
        if not selected_fields:
            flash('Please choose fields to generate spreadsheet.', 'warning')
            return redirect(url_for('export_data'))
            
        filter_category = request.form.get('filter_category')
        query = Record.query
        if filter_category != 'All': query = query.filter_by(category=filter_category)
        records = query.all()
        
        def generate():
            yield ','.join(selected_fields) + '\n'
            for r in records:
                row = []
                if 'ID' in selected_fields: row.append(str(r.id))
                if 'Date' in selected_fields: row.append(r.date.strftime('%Y-%m-%d'))
                if 'Activity' in selected_fields: row.append(r.category)
                if 'Type' in selected_fields: row.append(r.type)
                if 'Account_Type' in selected_fields: row.append(r.account_type.name if r.account_type else 'N/A')
                if 'Amount' in selected_fields: row.append(str(r.amount))
                if 'Description' in selected_fields: row.append(f'"{r.description}"')
                if 'Submitter' in selected_fields: row.append(r.submitter.username if r.submitter else 'System')
                if 'Status' in selected_fields: row.append(r.status)
                if 'Approver' in selected_fields: row.append(r.approver.username if r.approver else 'N/A')
                yield ','.join(row) + '\n'
                
        return Response(generate(), mimetype='text/csv', headers={"Content-Disposition": "attachment;filename=financial_report.csv"})
        
    return render_template('export.html')

# ==================== Master Identity Panel ====================
@app.route('/users', methods=['GET', 'POST'])
@login_required
def manage_users():
    if current_user.role != 'master':
        flash('Access Denied!', 'danger')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            username = request.form.get('username').strip()
            password = request.form.get('password')
            role = request.form.get('role')
            if User.query.filter_by(username=username).first():
                flash(f'Username "{username}" already exists!', 'danger')
            else:
                db.session.add(User(username=username, password=generate_password_hash(password), role=role))
                db.session.commit()
                flash(f'User "{username}" established.', 'success')
        elif action == 'update_password':
            u = User.query.get(request.form.get('user_id'))
            if u:
                u.password = generate_password_hash(request.form.get('new_password'))
                db.session.commit()
                flash(f'Password updated for {u.username}', 'success')
        return redirect(url_for('manage_users'))
    return render_template('users.html', users=User.query.all())

@app.route('/users/delete/<int:user_id>')
@login_required
def delete_user(user_id):
    if current_user.role != 'master': return redirect(url_for('dashboard'))
    u = User.query.get_or_404(user_id)
    if u.id == current_user.id: flash('Cannot delete yourself!', 'danger')
    else:
        db.session.delete(u)
        db.session.commit()
        flash('User purged.', 'success')
    return redirect(url_for('manage_users'))

# ==================== Master Account Types Configuration ====================
@app.route('/account-types', methods=['GET', 'POST'])
@login_required
def manage_account_types():
    if current_user.role != 'master':
        flash('Access Denied!', 'danger')
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        name = request.form.get('name').strip()
        if not name:
            flash('Account name cannot be hollow.', 'warning')
        elif AccountType.query.filter_by(name=name).first():
            flash(f'Archetype "{name}" already exists.', 'danger')
        else:
            db.session.add(AccountType(name=name))
            db.session.commit()
            flash(f'New account type "{name}" activated.', 'success')
        return redirect(url_for('manage_account_types'))
        
    types = AccountType.query.all()
    return render_template('account_types.html', types=types)

@app.route('/account-types/delete/<int:type_id>')
@login_required
def delete_account_type(type_id):
    if current_user.role != 'master': return redirect(url_for('dashboard'))
    t = AccountType.query.get_or_404(type_id)
    if Record.query.filter_by(account_type_id=type_id).first():
        flash(f'Integrity Block: Cannot delete "{t.name}". It contains active financial history!', 'danger')
    else:
        db.session.delete(t)
        db.session.commit()
        flash(f'Account type "{t.name}" discontinued.', 'success')
    return redirect(url_for('manage_account_types'))

# ==================== Database Setup & Seeding ====================
def init_db():
    db.create_all()
    
    # ⚡ 【已完全修复】采用高级原子隔离连接执行 DDL 迁移更新命令，彻底根除高并发多线程环境下的 500 连接僵死报错
    with db.engine.begin() as connection:
        try:
            connection.execute(db.text("ALTER TABLE record ADD COLUMN bank_submitter_id INTEGER;"))
            print("[Database Migrator] Column 'bank_submitter_id' patch successfully applied.")
        except Exception:
            print("[Database Migrator] Column 'bank_submitter_id' already exists. Skipping patch.")
            
    if not AccountType.query.first():
        db.session.add_all([AccountType(name='Cash (On-hand)'), AccountType(name='Bank Account'), AccountType(name='Digital App Wallet')]), db.session.commit()
        
    for u in [{'username': 'master1', 'role': 'master'}, {'username': 'finance1', 'role': 'accountant'}, {'username': 'finance2', 'role': 'accountant'}, {'username': 'member1', 'role': 'member'}]:
        if not User.query.filter_by(username=u['username']).first():
            db.session.add(User(username=u['username'], password=generate_password_hash('pass123'), role=u['role']))
    db.session.commit()

if __name__ == '__main__':
    with app.app_context(): init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
