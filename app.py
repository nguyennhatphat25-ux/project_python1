from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
import pymysql
import hashlib
import hmac
import json
from datetime import datetime
import io
import xlsxwriter
from functools import wraps
import requests
import uuid
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# ==============================
# 1. CẤU HÌNH HỆ THỐNG
# ==============================

# --- Cấu hình Upload Ảnh ---
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Tự động tạo thư mục nếu chưa có
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Cấu hình MySQL ---
MYSQL_CONFIG = {
    'host': os.environ.get('MYSQL_HOST', 'localhost'),
    'user': 'root',
    'password': '',  # <-- Điền mật khẩu MySQL của bạn
    'db': 'expense_splitter',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# --- Cấu hình MoMo (Port 5001) ---
MOMO_CONFIG = {
    'partnerCode': 'MOMOBKUN20180529',
    'accessKey': 'klm05TvNBzhg7h7j',
    'secretKey': 'at67qH6mk8w5Y1nAyMoYKMWACiEi2bsa',
    'endpoint': 'https://test-payment.momo.vn/v2/gateway/api/create',
    'redirectUrl': 'http://localhost:5001/momo-callback',
    'ipnUrl': 'http://localhost:5001/momo-ipn'
}

# ==============================
# 2. HÀM HỖ TRỢ (HELPERS)
# ==============================
def get_db():
    return pymysql.connect(**MYSQL_CONFIG)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Vui lòng đăng nhập!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def calculate_settlements(balances):
    debtors = []
    creditors = []
    for person, amount in balances.items():
        if amount < -1: debtors.append({'person': person, 'amount': amount})
        elif amount > 1: creditors.append({'person': person, 'amount': amount})
    debtors.sort(key=lambda x: x['amount'])
    creditors.sort(key=lambda x: x['amount'], reverse=True)
    settlements = []
    i = 0
    j = 0
    while i < len(debtors) and j < len(creditors):
        debtor = debtors[i]
        creditor = creditors[j]
        amount = min(abs(debtor['amount']), creditor['amount'])
        settlements.append({'from_user': debtor['person'], 'to_user': creditor['person'], 'amount': amount})
        debtor['amount'] += amount
        creditor['amount'] -= amount
        if abs(debtor['amount']) < 1: i += 1
        if creditor['amount'] < 1: j += 1
    return settlements

# ==============================
# 3. AUTHENTICATION
# ==============================
@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            flash('Email đã được đăng ký!', 'error')
            return redirect(url_for('register'))
            
        hashed = hashlib.sha256(password.encode()).hexdigest()
        cur.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)", (name, email, hashed))
        user_id = cur.lastrowid
        
        # Tự động tạo nhóm cá nhân
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)", ("Chi tiêu cá nhân", "VND", user_id))
        group_id = cur.lastrowid
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (group_id, name))
        
        conn.commit()
        cur.close()
        conn.close()
        flash('Đăng ký thành công!', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        hashed = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s AND password = %s", (email, hashed))
        user = cur.fetchone()
        cur.close()
        conn.close()
        
        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            # QUAN TRỌNG: Lưu avatar vào session để hiển thị ngay trên Menu
            session['user_avatar'] = user.get('avatar') 
            return redirect(url_for('dashboard'))
        else:
            flash('Sai email hoặc mật khẩu!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ==============================
# 4. DASHBOARD & PROFILE (Phần bạn cần)
# ==============================
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT g.*, COUNT(DISTINCT gm.id) AS member_count, COUNT(DISTINCT e.id) AS expense_count, COALESCE(SUM(e.amount), 0) AS total_amount
        FROM `groups` g LEFT JOIN group_members gm ON g.id = gm.group_id LEFT JOIN expenses e ON g.id = e.group_id
        WHERE g.created_by = %s GROUP BY g.id ORDER BY g.created_at DESC
    """, (session['user_id'],))
    groups = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('dashboard.jinja2', groups=groups)

# --- Route Xem Profile ---
@app.route('/profile')
@login_required
def profile():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (session['user_id'],))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return render_template('profile.html', user=user)

# --- Route Cập nhật Profile & Avatar ---
@app.route('/profile/update', methods=['POST'])
@login_required
def update_profile():
    name = request.form.get('name')
    phone = request.form.get('phone')
    address = request.form.get('address')
    avatar_filename = None
    
    # Xử lý file ảnh
    if 'avatar' in request.files:
        file = request.files['avatar']
        if file and file.filename != '' and allowed_file(file.filename):
            # Đổi tên file an toàn: user_ID_timestamp.jpg
            filename = secure_filename(file.filename)
            new_filename = f"user_{session['user_id']}_{int(datetime.now().timestamp())}.{filename.rsplit('.', 1)[1].lower()}"
            
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
            avatar_filename = new_filename

    conn = get_db()
    cur = conn.cursor()
    try:
        if avatar_filename:
            # Cập nhật cả ảnh
            cur.execute("""
                UPDATE users SET name=%s, phone=%s, address=%s, avatar=%s WHERE id=%s
            """, (name, phone, address, avatar_filename, session['user_id']))
            session['user_avatar'] = avatar_filename # Cập nhật session ngay lập tức
        else:
            # Chỉ cập nhật thông tin text
            cur.execute("""
                UPDATE users SET name=%s, phone=%s, address=%s WHERE id=%s
            """, (name, phone, address, session['user_id']))
        
        session['user_name'] = name
        conn.commit()
        flash('Cập nhật hồ sơ thành công!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Lỗi cập nhật: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
        
    return redirect(url_for('profile'))

# ==============================
# 5. GROUP & EXPENSE LOGIC
# ==============================
@app.route('/api/stats')
@login_required
def api_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.category, SUM(e.amount) as total FROM expenses e JOIN `groups` g ON e.group_id = g.id
        WHERE g.created_by = %s GROUP BY e.category
    """, (session['user_id'],))
    data = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(data)

@app.route('/api/stats/<int:group_id>')
@login_required
def api_group_stats(group_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    if not cur.fetchone(): return jsonify([])
    cur.execute("SELECT category, SUM(amount) as total FROM expenses WHERE group_id = %s GROUP BY category", (group_id,))
    data = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(data)

@app.route('/group/create', methods=['POST'])
@login_required
def create_group():
    name = request.form['name']
    currency = request.form['currency']
    members = [m.strip() for m in request.form.getlist('members[]') if m.strip()] 
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)", (name, currency, session['user_id']))
        gid = cur.lastrowid
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (gid, session['user_name']))
        for m in members: cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (gid, m))
        conn.commit()
    except: conn.rollback()
    finally: cur.close(); conn.close()
    return redirect(url_for('group_detail', group_id=gid))

@app.route('/group/<int:group_id>')
@login_required
def group_detail(group_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group: return redirect(url_for('dashboard'))
    
    is_admin = (group['created_by'] == session['user_id'])

    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()
    cur.execute("SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') as split_members FROM expenses e LEFT JOIN expense_splits es ON e.id = es.expense_id WHERE e.group_id = %s GROUP BY e.id ORDER BY e.date DESC", (group_id,))
    expenses = cur.fetchall()
    balances = {m['name']: 0.0 for m in members}
    for exp in expenses:
        amount = float(exp['amount'])
        paid_by = exp['paid_by']
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        if splits:
            per = amount / len(splits)
            if paid_by in balances: balances[paid_by] += amount
            for s in splits: 
                if s['member_name'] in balances: balances[s['member_name']] -= per
    for k in balances: balances[k] = round(balances[k], 0)
    settlements = calculate_settlements(balances)
    cur.close()
    conn.close()
    
    return render_template("group_detail.jinja2", group=group, members=members, balances=balances, expenses=expenses, settlements=settlements, is_admin=is_admin)

@app.route('/expense/create', methods=['POST'])
@login_required
def create_expense():
    group_id = request.form['group_id']
    description = request.form['description']
    amount = float(request.form['amount'])
    category = request.form['category']
    paid_by = request.form['paid_by']
    due_date = request.form.get('due_date') or None
    split_with = request.form.getlist('split_with[]') 
    
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO expenses (group_id, description, amount, category, paid_by, date, due_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (group_id, description, amount, category, paid_by, datetime.now(), due_date))
        eid = cur.lastrowid
        for m in split_with: cur.execute("INSERT INTO expense_splits (expense_id, member_name) VALUES (%s, %s)", (eid, m))
        conn.commit()
        flash("Thêm chi tiêu thành công!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Lỗi: {str(e)}", "error")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/expense/update/<int:expense_id>', methods=['POST'])
@login_required
def update_expense(expense_id):
    group_id = request.form['group_id']
    description = request.form['description']
    amount = float(request.form['amount'])
    category = request.form['category']
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT e.group_id, g.created_by FROM expenses e JOIN `groups` g ON e.group_id=g.id WHERE e.id=%s", (expense_id,))
    row = cur.fetchone()
    if row and row['created_by'] != session['user_id']:
        flash("Chỉ Admin mới được sửa!", "error")
        return redirect(url_for('group_detail', group_id=group_id))

    try:
        cur.execute("UPDATE expenses SET description=%s, amount=%s, category=%s WHERE id=%s", (description, amount, category, expense_id))
        conn.commit()
        flash("Cập nhật thành công!", "success")
    except: conn.rollback()
    finally: cur.close(); conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/expense/delete/<int:expense_id>', methods=['POST'])
@login_required
def delete_expense(expense_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT e.group_id, g.created_by FROM expenses e JOIN `groups` g ON e.group_id=g.id WHERE e.id=%s", (expense_id,))
    row = cur.fetchone()
    
    if row:
        if row['created_by'] != session['user_id']:
            flash("Chỉ Admin mới được xóa!", "error")
        else:
            cur.execute("DELETE FROM expenses WHERE id=%s", (expense_id,))
            conn.commit()
            flash("Đã xóa!", "success")
    cur.close()
    conn.close()
    return redirect(url_for('group_detail', group_id=row['group_id'] if row else None))

@app.route('/group/delete/<int:group_id>', methods=['POST'])
@login_required
def delete_group(group_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM `groups` WHERE id=%s AND created_by=%s", (group_id, session['user_id']))
    if cur.rowcount > 0:
        conn.commit()
        flash("Đã xóa nhóm!", "success")
    else:
        flash("Không thể xóa (Bạn không phải Admin)", "error")
    cur.close()
    conn.close()
    return redirect(url_for('dashboard'))

# ==============================
# 6. EXPORT EXCEL
# ==============================
@app.route('/export/<int:group_id>')
@login_required
def export_excel(group_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group: return redirect(url_for('dashboard'))
    
    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()
    cur.execute("""
        SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') as split_members
        FROM expenses e LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s GROUP BY e.id ORDER BY e.date DESC
    """, (group_id,))
    expenses = cur.fetchall()
    
    balances = {m['name']: 0.0 for m in members}
    for exp in expenses:
        amount = float(exp['amount'])
        paid_by = exp['paid_by']
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        if splits:
            per = amount / len(splits)
            if paid_by in balances: balances[paid_by] += amount
            for s in splits:
                if s['member_name'] in balances: balances[s['member_name']] -= per

    cur.close()
    conn.close()

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    bold = workbook.add_format({'bold': True})
    money_fmt = workbook.add_format({'num_format': '#,##0'})
    date_fmt = workbook.add_format({'num_format': 'dd/mm/yyyy'})

    sheet1 = workbook.add_worksheet("Chi tiêu")
    headers1 = ["Ngày", "Mô tả", "Danh mục", "Số tiền", "Người trả", "Chia cho"]
    for col, h in enumerate(headers1): sheet1.write(0, col, h, bold)
    
    for row, exp in enumerate(expenses, start=1):
        sheet1.write(row, 0, exp['date'], date_fmt)
        sheet1.write(row, 1, exp['description'])
        sheet1.write(row, 2, exp['category'])
        sheet1.write(row, 3, float(exp['amount']), money_fmt)
        sheet1.write(row, 4, exp['paid_by'])
        sheet1.write(row, 5, exp['split_members'])

    sheet2 = workbook.add_worksheet("Tổng kết")
    sheet2.write(0, 0, "Thành viên", bold)
    sheet2.write(0, 1, "Số dư", bold)
    sheet2.write(0, 2, "Trạng thái", bold)

    for row, (name, bal) in enumerate(balances.items(), start=1):
        sheet2.write(row, 0, name)
        sheet2.write(row, 1, bal, money_fmt)
        status = "Nhận lại" if bal > 0 else "Phải trả" if bal < 0 else "-"
        sheet2.write(row, 2, status)

    workbook.close()
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"Bao_cao_{group['name']}.xlsx")

# ==============================
# 7. MOMO PAYMENT
# ==============================
@app.route('/momo/create-payment', methods=['POST'])
@login_required
def create_momo_payment():
    try:
        data = request.json
        amount = str(int(data['amount']))
        order_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        
        group_id = str(data.get('groupId', ''))
        extra_data = group_id

        raw_sig = f"accessKey={MOMO_CONFIG['accessKey']}&amount={amount}&extraData={extra_data}&ipnUrl={MOMO_CONFIG['ipnUrl']}&orderId={order_id}&orderInfo=Pay&partnerCode={MOMO_CONFIG['partnerCode']}&redirectUrl={MOMO_CONFIG['redirectUrl']}&requestId={request_id}&requestType=captureWallet"
        signature = hmac.new(MOMO_CONFIG['secretKey'].encode(), raw_sig.encode(), hashlib.sha256).hexdigest()
        
        payload = {
            'partnerCode': MOMO_CONFIG['partnerCode'], 'requestId': request_id, 'amount': amount, 'orderId': order_id, 
            'orderInfo': 'Pay', 'redirectUrl': MOMO_CONFIG['redirectUrl'], 'ipnUrl': MOMO_CONFIG['ipnUrl'], 
            'extraData': extra_data, 'requestType': 'captureWallet', 'signature': signature, 'lang': 'vi'
        }
        res = requests.post(MOMO_CONFIG['endpoint'], json=payload)
        return jsonify(res.json())
    except Exception as e: return jsonify({'errorCode': -1, 'message': str(e)})

@app.route('/momo-callback')
def momo_callback():
    resultCode = request.args.get("resultCode")
    group_id_str = request.args.get("extraData")
    
    if resultCode == "0": flash("Thanh toán thành công!", "success")
    else: flash("Thanh toán thất bại!", "error")
    
    if group_id_str and group_id_str.isdigit():
        return redirect(url_for('group_detail', group_id=int(group_id_str)))
    return redirect(url_for("dashboard"))

@app.route('/momo-ipn', methods=['POST'])
def momo_ipn(): return jsonify({"message": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)