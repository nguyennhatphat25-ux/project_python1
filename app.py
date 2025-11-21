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

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# ==============================
# Cấu hình (Configuration)
# ==============================
MYSQL_CONFIG = {    
    'host': 'localhost',
    'user': 'root',
    'password': '',  # Điền mật khẩu MySQL của bạn nếu có
    'password': '',  # <-- Điền mật khẩu MySQL của bạn
    'db': 'expense_splitter',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

MOMO_CONFIG = {
    'partnerCode': 'MOMOBKUN20180529',
    'accessKey': 'klm05TvNBzhg7h7j',
    'secretKey': 'at67qH6mk8w5Y1nAyMoYKMWACiEi2bsa',
    'endpoint': 'https://test-payment.momo.vn/v2/gateway/api/create',
    'redirectUrl': 'http://localhost:5000/momo-callback',
    'ipnUrl': 'http://localhost:5000/momo-ipn'
}

# ==============================
# Hàm hỗ trợ (Helpers)
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
        settlements.append({
            'from_user': debtor['person'],
            'to_user': creditor['person'],
            'amount': amount
        })
        debtor['amount'] += amount
        creditor['amount'] -= amount
        if abs(debtor['amount']) < 1: i += 1
        if creditor['amount'] < 1: j += 1
    return settlements

# ==============================
# Authentication Routes
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
        
        # TỰ ĐỘNG TẠO NHÓM "CHI TIÊU CÁ NHÂN"
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)", 
                    ("Chi tiêu cá nhân", "VND", user_id))
        group_id = cur.lastrowid
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (group_id, name))
        
        conn.commit()
        cur.close()
        conn.close()
        flash('Đăng ký thành công! Đã tạo sẵn nhóm cá nhân cho bạn.', 'success')
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
            flash('Đăng nhập thành công!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Sai email hoặc mật khẩu!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Đã đăng xuất!', 'success')
    return redirect(url_for('login'))

# ==============================
# Dashboard & Group Logic
# ==============================
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT g.*, 
               COUNT(DISTINCT gm.id) AS member_count,
               COUNT(DISTINCT e.id) AS expense_count,
               COALESCE(SUM(e.amount), 0) AS total_amount
        FROM `groups` g
        LEFT JOIN group_members gm ON g.id = gm.group_id
        LEFT JOIN expenses e ON g.id = e.group_id
        WHERE g.created_by = %s
        GROUP BY g.id
        ORDER BY g.created_at DESC
    """, (session['user_id'],))
    groups = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('dashboard.jinja2', groups=groups)

# --- API Chart Tổng Quát (Dashboard) ---
@app.route('/api/stats')
@login_required
def api_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.category, SUM(e.amount) as total
        FROM expenses e
        JOIN `groups` g ON e.group_id = g.id
        WHERE g.created_by = %s
        GROUP BY e.category
    """, (session['user_id'],))
    data = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(data)

# --- [MỚI] API Chart Theo Từng Nhóm ---
@app.route('/api/stats/<int:group_id>')
@login_required
def api_group_stats(group_id):
    conn = get_db()
    cur = conn.cursor()
    # Kiểm tra quyền truy cập (nếu cần thiết, ở đây mình check đơn giản)
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group:
        return jsonify([]) # Trả về rỗng nếu không thấy nhóm

    # Lấy thống kê chi tiêu của nhóm này
    cur.execute("""
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE group_id = %s
        GROUP BY category
    """, (group_id,))
    data = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(data)

@app.route('/group/create', methods=['POST'])
@login_required
def create_group():
    name = request.form['name']
    currency = request.form['currency']
    members = request.form.getlist('members[]')
    members = [m.strip() for m in members if m.strip()] 
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)",
                    (name, currency, session['user_id']))
        group_id = cur.lastrowid
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", 
                    (group_id, session['user_name']))
        for m in members:
            cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (group_id, m))
        conn.commit()
        flash('Tạo nhóm thành công!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Lỗi: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>')
@login_required
def group_detail(group_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group:
        flash("Nhóm không tồn tại!", "error")
        return redirect(url_for('dashboard'))
    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()
    cur.execute("""
        SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') as split_members
        FROM expenses e
        LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s
        GROUP BY e.id
        ORDER BY e.date DESC
    """, (group_id,))
    expenses = cur.fetchall()
    balances = {m['name']: 0.0 for m in members}
    for exp in expenses:
        amount = float(exp['amount'])
        paid_by = exp['paid_by']
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        if splits:
            per_person = amount / len(splits)
            if paid_by in balances: balances[paid_by] += amount
            for s in splits:
                m_name = s['member_name']
                if m_name in balances: balances[m_name] -= per_person
    for k in balances: balances[k] = round(balances[k], 0)
    settlements = calculate_settlements(balances)
    cur.close()
    conn.close()
    return render_template("group_detail.jinja2",
                           group=group,
                           members=members,
                           balances=balances,
                           expenses=expenses,
                           settlements=settlements)

@app.route('/expense/create', methods=['POST'])
@login_required
def create_expense():
    group_id = request.form['group_id']
    description = request.form['description']
    amount = float(request.form['amount'])
    category = request.form['category']
    paid_by = request.form['paid_by']
    split_with = request.form.getlist('split_with[]') 
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO expenses (group_id, description, amount, category, paid_by, date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (group_id, description, amount, category, paid_by, datetime.now()))
        expense_id = cur.lastrowid
        for member in split_with:
            cur.execute("INSERT INTO expense_splits (expense_id, member_name) VALUES (%s, %s)",
                        (expense_id, member))
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
    try:
        cur.execute("""
            UPDATE expenses 
            SET description=%s, amount=%s, category=%s 
            WHERE id=%s
        """, (description, amount, category, expense_id))
        conn.commit()
        flash("Cập nhật thành công!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Lỗi cập nhật: {str(e)}", "error")
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/expense/delete/<int:expense_id>', methods=['POST'])
@login_required
def delete_expense(expense_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT group_id FROM expenses WHERE id = %s", (expense_id,))
        row = cur.fetchone()
        if not row: return redirect(url_for('dashboard'))
        group_id = row['group_id']
        cur.execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
        conn.commit()
        flash('Đã xóa chi tiêu!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Lỗi: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/delete/<int:group_id>', methods=['POST'])
@login_required
def delete_group(group_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT created_by FROM `groups` WHERE id = %s", (group_id,))
        row = cur.fetchone()
        if not row or row['created_by'] != session['user_id']:
            flash('Không có quyền xóa!', 'error')
            return redirect(url_for('dashboard'))
        cur.execute("DELETE FROM `groups` WHERE id = %s", (group_id,))
        conn.commit()
        flash('Đã xóa nhóm!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Lỗi: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('dashboard'))

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
        FROM expenses e
        LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s
        GROUP BY e.id
        ORDER BY e.date DESC
    """, (group_id,))
    expenses = cur.fetchall()
    balances = {m['name']: 0.0 for m in members}
    for exp in expenses:
        amount = float(exp['amount'])
        paid_by = exp['paid_by']
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        if splits:
            per_person = amount / len(splits)
            if paid_by in balances: balances[paid_by] += amount
            for s in splits:
                m_name = s['member_name']
                if m_name in balances: balances[m_name] -= per_person
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
    for row, (name, bal) in enumerate(balances.items(), start=1):
        sheet2.write(row, 0, name)
        sheet2.write(row, 1, bal, money_fmt)
    workbook.close()
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"Bao_cao_{group['name']}.xlsx")

@app.route('/momo/create-payment', methods=['POST'])
@login_required
def create_momo_payment():
    try:
        data = request.json
        amount = str(int(data['amount'])) 
        order_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        order_info = f"Thanh toan cho {data['to']}"
        raw_signature = f"accessKey={MOMO_CONFIG['accessKey']}&amount={amount}&extraData=&ipnUrl={MOMO_CONFIG['ipnUrl']}&orderId={order_id}&orderInfo={order_info}&partnerCode={MOMO_CONFIG['partnerCode']}&redirectUrl={MOMO_CONFIG['redirectUrl']}&requestId={request_id}&requestType=captureWallet"
        signature = hmac.new(MOMO_CONFIG['secretKey'].encode(), raw_signature.encode(), hashlib.sha256).hexdigest()
        payload = {
            'partnerCode': MOMO_CONFIG['partnerCode'], 'partnerName': "Expense Splitter", 'storeId': "MomoTestStore",
            'requestId': request_id, 'amount': amount, 'orderId': order_id, 'orderInfo': order_info,
            'redirectUrl': MOMO_CONFIG['redirectUrl'], 'ipnUrl': MOMO_CONFIG['ipnUrl'], 'lang': 'vi',
            'extraData': "", 'requestType': "captureWallet", 'signature': signature
        }
        response = requests.post(MOMO_CONFIG['endpoint'], json=payload)
        return jsonify(response.json())
    except Exception as e:
        return jsonify({'errorCode': -1, 'message': str(e)})

@app.route('/momo-callback')
def momo_callback():
    resultCode = request.args.get("resultCode")
    if resultCode == "0": flash("Thanh toán thành công!", "success")
    else: flash("Thanh toán thất bại!", "error")
    return redirect(url_for("dashboard"))

@app.route('/momo-ipn', methods=['POST'])
def momo_ipn(): return jsonify({"message": "success"})

if __name__ == '__main__':
    app.run(debug=True)