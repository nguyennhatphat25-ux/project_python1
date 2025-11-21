from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///data.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)  # <-- gắn trực tiếp với app

from models import Group, Member, Expense
from decimal import Decimal
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
import pymysql
import hashlib
import hmac
import json
from datetime import datetime
import io
import xlsxwriter
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# MySQL Configuration
MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',  
    'db': 'expense_splitter',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# MoMo Configuration
MOMO_CONFIG = {
    'partnerCode': 'MOMOBKUN20180529',
    'accessKey': 'klm05TvNBzhg7h7j',
    'secretKey': 'at67qH6mk8w5Y1nAyMoYKMWACiEi2bsa',
    'endpoint': 'https://test-payment.momo.vn/v2/gateway/api/create',
    'redirectUrl': 'http://localhost:5000/momo-callback',
    'ipnUrl': 'http://localhost:5000/momo-ipn'
}

# ==============================
# Database helper
# ==============================
def get_db():
    return pymysql.connect(**MYSQL_CONFIG)

# ==============================
# Login Required Decorator
# ==============================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Vui lòng đăng nhập!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ==============================
# Authentication
# ==============================
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
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
        user = cur.fetchone()

        if user:
            flash('Email đã được đăng ký!', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('register'))

        hashed = hashlib.sha256(password.encode()).hexdigest()
        cur.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                    (name, email, hashed))
        conn.commit()
        cur.close()
        conn.close()

        flash('Đăng ký thành công! Vui lòng đăng nhập.', 'success')
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

        cur.execute("SELECT * FROM users WHERE email = %s AND password = %s",
                    (email, hashed))
        user = cur.fetchone()

        cur.close()
        conn.close()

        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            flash('Đăng nhập thành công!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Email hoặc mật khẩu không đúng!', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Đã đăng xuất!', 'success')
    return redirect(url_for('login'))


# ==============================
# Dashboard
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


# ==============================
# Create Group
# ==============================
@app.route('/group/create', methods=['POST'])
@login_required
def create_group():
    name = request.form['name']
    currency = request.form['currency']
    members_raw = request.form.get('members', '')
    members = [m.strip() for m in members_raw.split(',') if m.strip()]

    conn = get_db()
    cur = conn.cursor()

    # Tạo nhóm
    cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)",
                (name, currency, session['user_id']))
    group_id = cur.lastrowid

    # Thêm thành viên
    for m in members:
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)",
                    (group_id, m))

    conn.commit()
    cur.close()
    conn.close()

    flash('Tạo nhóm thành công!', 'success')
    return redirect(url_for('group_detail', group_id=group_id))


# ==============================
# Group Detail
# ==============================
@app.route('/group/<int:group_id>')
@login_required
def group_detail(group_id):
    group = Group.query.get_or_404(group_id)
    members = group.members

    # Khởi tạo balances
    balances = {member.name: 0.0 for member in members}  # float

    expenses = [
        {"paid_by": exp.paid_by, "amount": exp.amount} 
        for exp in group.expenses
    ]

    for expense in expenses:
        balances[expense["paid_by"]] += float(expense["amount"])  # <-- convert Decimal → float

    return render_template("group_detail.jinja2",
                           group=group,
                           members=members,
                           balances=balances,
                           expenses=group.expenses)


# ==============================
# Add Expense
# ==============================
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

    cur.execute("""
        INSERT INTO expenses (group_id, description, amount, category, paid_by, date)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (group_id, description, amount, category, paid_by, datetime.now()))

    expense_id = cur.lastrowid

    for member in split_with:
        cur.execute("INSERT INTO expense_splits (expense_id, member_name) VALUES (%s, %s)",
                    (expense_id, member))

    conn.commit()
    cur.close()
    conn.close()

    flash("Thêm chi tiêu thành công!", "success")
    return redirect(url_for('group_detail', group_id=group_id))


# ==============================
# Export Excel
# ==============================
@app.route('/export/<int:group_id>')
@login_required
def export_excel(group_id):

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM `groups` WHERE id = %s AND created_by = %s",
                (group_id, session['user_id']))
    group = cur.fetchone()

    if not group:
        flash("Nhóm không tồn tại!", "error")
        return redirect(url_for('dashboard'))

    cur.execute("""
        SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') AS split_members
        FROM expenses e
        LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s
        GROUP BY e.id
    """, (group_id,))
    expenses = cur.fetchall()

    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()

    balances = {m["name"]: 0 for m in members}

    for expense in expenses:
        cur.execute("SELECT * FROM expense_splits WHERE expense_id = %s",
                    (expense['id'],))
        splits = cur.fetchall()

        if splits:
            per = expense["amount"] / len(splits)
            balances[expense["paid_by"]] += expense["amount"]
            for s in splits:
                balances[s["member_name"]] -= per

    cur.close()
    conn.close()

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)

    # Sheet Chi tiêu
    sheet = workbook.add_worksheet("Chi tiêu")
    headers = ["Ngày", "Mô tả", "Danh mục", "Số tiền", "Người trả", "Chia cho"]
    for i, h in enumerate(headers):
        sheet.write(0, i, h)

    for idx, e in enumerate(expenses, start=1):
        sheet.write(idx, 0, e["date"].strftime("%d/%m/%Y"))
        sheet.write(idx, 1, e["description"])
        sheet.write(idx, 2, e["category"])
        sheet.write(idx, 3, e["amount"])
        sheet.write(idx, 4, e["paid_by"])
        sheet.write(idx, 5, e["split_members"])

    # Sheet Số dư
    sheet2 = workbook.add_worksheet("Số dư")
    sheet2.write(0, 0, "Thành viên")
    sheet2.write(0, 1, "Số dư")
    sheet2.write(0, 2, "Trạng thái")

    i = 1
    for member, bal in balances.items():
        sheet2.write(i, 0, member)
        sheet2.write(i, 1, bal)
        sheet2.write(i, 2, "Được trả" if bal >= 0 else "Cần trả")
        i += 1

    workbook.close()
    output.seek(0)

    return send_file(output,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"{group['name']}_{datetime.now().strftime('%Y%m%d')}.xlsx")


# ==============================
# MoMo Payment
# ==============================
@app.route('/momo/create-payment', methods=['POST'])
@login_required
def create_momo_payment():
    data = request.json
    order_id = f"ORDER_{int(datetime.now().timestamp())}"

    amount = int(data["amount"])
    order_info = f"Thanh toán từ {data['from']} cho {data['to']}"

    raw = (
        f"accessKey={MOMO_CONFIG['accessKey']}"
        f"&amount={amount}"
        f"&extraData="
        f"&ipnUrl={MOMO_CONFIG['ipnUrl']}"
        f"&orderId={order_id}"
        f"&orderInfo={order_info}"
        f"&partnerCode={MOMO_CONFIG['partnerCode']}"
        f"&redirectUrl={MOMO_CONFIG['redirectUrl']}"
        f"&requestId={order_id}"
        f"&requestType=captureWallet"
    )

    signature = hmac.new(
        MOMO_CONFIG['secretKey'].encode(),
        raw.encode(),
        hashlib.sha256
    ).hexdigest()

    return jsonify({
        "success": True,
        "paymentUrl": MOMO_CONFIG["endpoint"],
        "orderId": order_id,
        "debugSignature": signature
    })


@app.route('/momo-callback')
def momo_callback():
    resultCode = request.args.get("resultCode")

    if resultCode == "0":
        flash("Thanh toán thành công!", "success")
    else:
        flash("Thanh toán thất bại!", "error")

    return redirect(url_for("dashboard"))


@app.route('/momo-ipn', methods=['POST'])
def momo_ipn():
    return jsonify({"message": "success"})


# ==============================
# Settlement Calculation
# ==============================
def calculate_settlements(balances):

    settlements = []

    debtors = [(name, bal) for name, bal in balances.items() if bal < 0]
    creditors = [(name, bal) for name, bal in balances.items() if bal > 0]

    debtors.sort(key=lambda x: x[1])         # tăng dần (âm nhiều nhất trước)
    creditors.sort(key=lambda x: x[1], reverse=True)  # giảm dần

    i = j = 0

    while i < len(debtors) and j < len(creditors):

        debtor, debt = debtors[i]
        creditor, credit = creditors[j]

        pay = min(abs(debt), credit)

        settlements.append({
            "from": debtor,
            "to": creditor,
            "amount": pay
        })

        debtors[i] = (debtor, debt + pay)      # debt + (positive)
        creditors[j] = (creditor, credit - pay)

        if abs(debtors[i][1]) < 0.01:
            i += 1
        if creditors[j][1] < 0.01:
            j += 1

    return settlements


# ==============================
# Start App
# ==============================
if __name__ == '__main__':
    app.run(debug=True)

