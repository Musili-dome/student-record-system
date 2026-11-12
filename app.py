from flask import Flask, render_template, request, redirect, session, flash, Response, abort
import mysql.connector
import hashlib
import csv
import io
import os
import secrets
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-to-something-secret")

# Email settings. The password is read from the environment, never typed into this file.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")


def get_db():
    return mysql.connector.connect(
        host=os.environ.get("MYSQLHOST", "localhost"),
        user=os.environ.get("MYSQLUSER", "root"),
        password=os.environ.get("MYSQLPASSWORD", ""),
        database=os.environ.get("MYSQLDATABASE", "railway"),
        port=int(os.environ.get("MYSQLPORT", 3306))
    )


@app.after_request
def no_cache(response):
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response



# ---------- Hash Table + Linked List ----------
class HashTableIndex:
    def __init__(self, size=101):
        self.size = size
        self.buckets = [[] for _ in range(size)]

    def _hash(self, key):
        return hash(key) % self.size

    def insert(self, key, record):
        idx = self._hash(key)
        for i, (k, _) in enumerate(self.buckets[idx]):
            if k == key:
                self.buckets[idx][i] = (key, record)
                return
        self.buckets[idx].append((key, record))

    def delete(self, key):
        idx = self._hash(key)
        self.buckets[idx] = [(k, v) for k, v in self.buckets[idx] if k != key]

    def search(self, key):
        idx = self._hash(key)
        for k, record in self.buckets[idx]:
            if k == key:
                return record
        return None


class Node:
    def __init__(self, student):

        self.student = student
        self.next = None


class LinkedListStore:
    def __init__(self):
        self.head = None

    def traverse_check_duplicate(self, admission_no):
        current = self.head
        while current:
            if current.student["admission_no"] == admission_no:
                return True
            current = current.next
        return False

    def add_record(self, student):
        if self.traverse_check_duplicate(student["admission_no"]):
            return False
        node = Node(student)
        node.next = self.head
        self.head = node
        return True

    def remove_record(self, admission_no):
        current, prev = self.head, None
        while current:
            if current.student["admission_no"] == admission_no:
                if prev:
                    prev.next = current.next
                else:
                    self.head = current.next

                return True
            prev, current = current, current.next
        return False


index = HashTableIndex()
store = LinkedListStore()


def load_structures_from_db():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""SELECT student_id, full_name, admission_no, course,
                             year_of_study, contact, email
                      FROM students""")
    for row in cursor.fetchall():
        store.add_record(row)
        index.insert(row["admission_no"], row)
    conn.close()


def valid_email(value):
    return (bool(value) and " " not in value and value.count("@") == 1
            and "." in value.split("@")[1])

# ---------- Account lockout ----------
MAX_ATTEMPTS = 3
LOCK_MINUTES = 15


def send_email(to_email, subject, body):
    """Returns True if sent, False if email isn't configured or fails."""
    if not (SMTP_USER and SMTP_PASSWORD and to_email):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print("Email sending failed:", e)
        return False


def notify_admin_lockout(account_label):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM users WHERE email IS NOT NULL AND email <> '' ORDER BY user_id LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    admin_email = row[0] if row else None

    body = (f"{account_label} was locked for {LOCK_MINUTES} minutes after "
            f"{MAX_ATTEMPTS} failed login attempts.\n\n"
            "If this was not the account owner, someone may be trying to guess the password.")
    if not send_email(admin_email, "Account locked - Student Record Management System", body):
        print(f"[ADMIN NOTICE] {account_label} was locked after {MAX_ATTEMPTS} failed login attempts.")


def verify_with_lockout(table, key_col, key, password):
    """Returns (status, row). status is 'ok', 'bad', 'locked' or 'just_locked'."""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"""
        SELECT *,
               (locked_until IS NOT NULL AND locked_until > NOW()) AS is_locked,
               CEIL(TIMESTAMPDIFF(SECOND, NOW(), locked_until) / 60) AS mins_left
        FROM {table} WHERE {key_col} = %s""", (key,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return "bad", None
    if row["is_locked"]:
        conn.close()
        return "locked", row
    if row["password_hash"] == hashlib.sha256(password.encode()).hexdigest():
        cursor.execute(f"UPDATE {table} SET failed_attempts = 0, locked_until = NULL WHERE {key_col} = %s", (key,))
        conn.commit()
        conn.close()
        return "ok", row

    attempts = (row["failed_attempts"] or 0) + 1
    if attempts >= MAX_ATTEMPTS:

        cursor.execute(f"""UPDATE {table}
                           SET failed_attempts = 0, locked_until = DATE_ADD(NOW(), INTERVAL %s MINUTE)
                           WHERE {key_col} = %s""", (LOCK_MINUTES, key))
        status = "just_locked"
    else:
        cursor.execute(f"UPDATE {table} SET failed_attempts = %s WHERE {key_col} = %s", (attempts, key))
        status = "bad"
    conn.commit()
    conn.close()
    return status, row


def lockout_message(status, row, who):
    if status == "locked":
        return f"Account locked. Try again in {int(row['mins_left'])} minute(s), or reset your password."
    if status == "just_locked":
        return f"Too many failed attempts. Account locked for {LOCK_MINUTES} minutes."
    return f"Invalid {who} or password"

# ---------- Admin login / dashboard ----------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        status, user = verify_with_lockout("users", "username", username, password)
        if status == "ok":
            session.clear()
            session["user_id"] = user["user_id"]
            return redirect("/dashboard")
        flash(lockout_message(status, user, "username"))
        if status == "just_locked":
            notify_admin_lockout(f"Admin account '{username}'")
    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT NULLIF(course, '')) FROM students")
    total_students, total_courses = cursor.fetchone()
    conn.close()
    return render_template("dashboard.html",
                           total_students=total_students,
                           total_courses=total_courses)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")



# ---------- Student record management (admin) ----------
def read_form():
    return {
        "full_name": request.form.get("full_name", "").strip(),
        "admission_no": request.form.get("admission_no", "").strip(),
        "course": request.form.get("course", "").strip(),
        "year_of_study": request.form.get("year_of_study", "").strip(),
        "contact": request.form.get("contact", "").strip(),
        "email": request.form.get("email", "").strip(),
    }


@app.route("/records")
def records():
    if "user_id" not in session:
        return redirect("/")
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM students ORDER BY student_id DESC")
    all_students = cursor.fetchall()
    conn.close()
    editing = None
    edit_key = request.args.get("edit")
    if edit_key:
        editing = index.search(edit_key)
    return render_template("records.html", students=all_students, editing=editing)


@app.route("/records/add", methods=["POST"])
def add_record():
    if "user_id" not in session:

        return redirect("/")
    student = read_form()
    if not student["full_name"] or not student["admission_no"]:
        flash("Full name and admission number are required", "error")
        return redirect("/records")
    if not valid_email(student["email"]):
        flash("A valid email address is required", "error")
        return redirect("/records")
    if store.traverse_check_duplicate(student["admission_no"]):
        flash("Duplicate admission number - record rejected", "error")
        return redirect("/records")

    default_hash = hashlib.sha256(student["admission_no"].encode()).hexdigest()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO students (full_name, admission_no, course, year_of_study,
                                  contact, email, password_hash)
            VALUES (%(full_name)s, %(admission_no)s, %(course)s, %(year_of_study)s,
                    %(contact)s, %(email)s, %(password_hash)s)
        """, {**student, "password_hash": default_hash})
        conn.commit()
        store.add_record(student)
        index.insert(student["admission_no"], student)
        flash("Record added successfully", "success")
    except mysql.connector.IntegrityError:
        flash("Duplicate admission number - record rejected", "error")
    conn.close()
    return redirect("/records")



@app.route("/records/update", methods=["POST"])
def update_record():
    if "user_id" not in session:
        return redirect("/")
    student = read_form()
    original = request.form.get("original_admission_no", "").strip() or student["admission_no"]
    new_adm = student["admission_no"]

    if not original or index.search(original) is None:
        flash("No record found with that admission number", "error")
        return redirect("/records")
    if not new_adm or not student["full_name"]:
        flash("Full name and admission number are required", "error")
        return redirect(f"/records?edit={original}")
    if not valid_email(student["email"]):
        flash("A valid email address is required", "error")
        return redirect(f"/records?edit={original}")
    if new_adm != original and store.traverse_check_duplicate(new_adm):
        flash("That admission number already belongs to another student", "error")
        return redirect(f"/records?edit={original}")

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE students
            SET admission_no=%(new_adm)s, full_name=%(full_name)s, course=%(course)s,
                year_of_study=%(year_of_study)s, contact=%(contact)s, email=%(email)s
            WHERE admission_no=%(original)s
        """, {**student, "new_adm": new_adm, "original": original})
        if new_adm != original:
            cursor.execute("""UPDATE password_resets SET used = 1

                              WHERE account_type = 'student'
                                AND account_key = %s AND used = 0""", (original,))
        conn.commit()
    except mysql.connector.IntegrityError:
        conn.rollback()
        conn.close()
        flash("That admission number already belongs to another student", "error")
        return redirect(f"/records?edit={original}")
    conn.close()

    store.remove_record(original)
    index.delete(original)
    store.add_record(student)
    index.insert(new_adm, student)
    flash("Record updated successfully", "success")
    return redirect("/records")



@app.route("/records/delete", methods=["POST"])
def delete_record():
    if "user_id" not in session:
        return redirect("/")
    admission_no = (request.form.get("original_admission_no", "").strip()
                    or request.form.get("admission_no", "").strip())
    if not admission_no or index.search(admission_no) is None:
        flash("No record found with that admission number", "error")
        return redirect("/records")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM students WHERE admission_no = %s", (admission_no,))
    conn.commit()
    conn.close()
    store.remove_record(admission_no)
    index.delete(admission_no)
    flash("Record deleted", "success")
    return redirect("/records")


# ---------- Search ----------
def find_by_name(text):
    """Linked list traversal with a partial, case-insensitive match."""
    text = text.lower()
    matches = []
    current = store.head
    while current:
        if text in (current.student.get("full_name") or "").lower():
            matches.append(current.student)
        current = current.next
    return matches


def digits_only(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


def find_by_contact(text):
    """Linked list traversal comparing phone numbers by digits only."""
    wanted = digits_only(text)
    matches = []
    if not wanted:
        return matches
    current = store.head
    while current:
        if wanted in digits_only(current.student.get("contact")):
            matches.append(current.student)
        current = current.next
    return matches


@app.route("/search", methods=["GET", "POST"])

def search():
    if "user_id" not in session:
        return redirect("/")
    results = []
    searched = False
    query = ""
    field = "admission_no"
    if request.method == "POST":
        searched = True
        field = request.form.get("field", "admission_no")
        query = request.form.get("query", "").strip()
        if query:
            if field == "admission_no":
                match = index.search(query)  # hash table lookup
                results = [match] if match else []
            elif field == "full_name":
                results = find_by_name(query)
            elif field == "contact":
                results = find_by_contact(query)
    return render_template("search.html", results=results, searched=searched,
                           query=query, field=field)



# ---------- Reports ----------
def fetch_report_students(course):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    if course:
        cursor.execute("SELECT * FROM students WHERE course = %s ORDER BY full_name", (course,))
    else:
        cursor.execute("SELECT * FROM students ORDER BY full_name")
    students = cursor.fetchall()
    conn.close()
    return students


@app.route("/reports")
def reports():
    if "user_id" not in session:
        return redirect("/")
    selected = request.args.get("course", "").strip()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT DISTINCT course FROM students WHERE course IS NOT NULL AND course <> '' ORDER BY course")
    courses = [row["course"] for row in cursor.fetchall()]
    conn.close()
    students = fetch_report_students(selected)
    return render_template("reports.html", students=students, courses=courses, selected=selected)


@app.route("/reports/export")
def export_report():
    if "user_id" not in session:
        return redirect("/")
    selected = request.args.get("course", "").strip()
    students = fetch_report_students(selected)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Admission No.", "Full Name", "Course", "Year of Study", "Contact", "Email"])
    for s in students:
        writer.writerow([s["admission_no"], s["full_name"], s["course"],
                         s["year_of_study"], s["contact"], s["email"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=student_report.csv"},
    )


# ---------- Student portal ----------
def current_student():
    adm = session.get("student_adm")
    return index.search(adm) if adm else None


@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        adm = request.form["admission_no"].strip()
        password = request.form["password"]
        status, row = verify_with_lockout("students", "admission_no", adm, password)
        if status == "ok":
            session.clear()
            session["student_adm"] = row["admission_no"]
            return redirect("/student/dashboard")
        flash(lockout_message(status, row, "admission number"))
        if status == "just_locked":
            notify_admin_lockout(f"Student account '{adm}'")
    return render_template("student_login.html")


@app.route("/student/dashboard")
def student_dashboard():
    student = current_student()
    if not student:
        return redirect("/student/login")
    return render_template("student_dashboard.html", student=student)


@app.route("/student/record")
def student_record():
    student = current_student()
    if not student:
        return redirect("/student/login")
    return render_template("student_record.html", student=student)


@app.route("/student/record/update", methods=["POST"])
def student_update_contact():
    student = current_student()
    if not student:
        return redirect("/student/login")
    contact = request.form.get("contact", "").strip()
    if not contact:

        flash("Contact cannot be empty", "error")
        return redirect("/student/record")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE students SET contact = %s WHERE admission_no = %s",
                   (contact, student["admission_no"]))
    conn.commit()
    conn.close()

    updated = dict(student)
    updated["contact"] = contact
    store.remove_record(student["admission_no"])
    store.add_record(updated)
    index.insert(updated["admission_no"], updated)
    flash("Contact details updated", "success")
    return redirect("/student/record")


@app.route("/student/report")
def student_report():
    student = current_student()
    if not student:
        return redirect("/student/login")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Admission No.", "Full Name", "Course", "Year of Study", "Contact", "Email"])
    writer.writerow([student["admission_no"], student["full_name"], student["course"],
                     student["year_of_study"], student["contact"], student.get("email")])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=my_record.csv"})



@app.route("/student/logout")
def student_logout():
    session.clear()
    return redirect("/student/login")


# ---------- Forgot / Reset Password (by email) ----------
RESET_ACCOUNTS = {
    "admin":   {"table": "users",    "key_col": "username",     "login_url": "/"},
    "student": {"table": "students", "key_col": "admission_no", "login_url": "/student/login"},
}


def send_reset_email(to_email, link):
    """Returns True if the email was sent, False if email is not configured or failed."""
    if not (SMTP_USER and SMTP_PASSWORD and to_email):
        return False
    msg = EmailMessage()
    msg["Subject"] = "Password reset - Student Record Management System"
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.set_content(
        "A password reset was requested for your account.\n\n"
        f"Open this link within 15 minutes to choose a new password:\n{link}\n\n"
        "If you did not request this, ignore this email. Your password will not change."
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        return True
    except Exception as e:
        print("Email sending failed:", e)
        return False


def start_reset(account_type, account_key):
    cfg = RESET_ACCOUNTS[account_type]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT email FROM {cfg['table']} WHERE {cfg['key_col']} = %s", (account_key,))
    row = cursor.fetchone()
    if row:
        email = row[0]
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        cursor.execute("""UPDATE password_resets SET used = 1
                          WHERE account_type = %s AND account_key = %s AND used = 0""",
                       (account_type, account_key))
        cursor.execute("""INSERT INTO password_resets (account_type, account_key, token_hash, expires_at)
                          VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL 15 MINUTE))""",
                       (account_type, account_key, token_hash))
        conn.commit()
        link = f"{request.host_url}reset/{account_type}/{token}"
        if not send_reset_email(email, link):
            # Fallback so the prototype still works without email set up
            print("\n" + "=" * 60)
            print(f"EMAIL NOT SENT. Reset link for {account_key} (valid 15 minutes):")
            print(link)
            print("=" * 60 + "\n")
    conn.close()



GENERIC_RESET_MESSAGE = "If that account exists, a reset link has been sent to its email address."


@app.route("/forgot-password", methods=["GET", "POST"])
def admin_forgot_password():
    if request.method == "POST":
        key = request.form.get("username", "").strip()
        if key:
            start_reset("admin", key)
        flash(GENERIC_RESET_MESSAGE, "success")
        return redirect("/forgot-password")
    return render_template("forgot_password.html", field_label="Username", field_name="username",
                           placeholder="e.g. admin01", action="/forgot-password",
                           back_url="/", bar_class="")


@app.route("/student/forgot-password", methods=["GET", "POST"])
def student_forgot_password():
    if request.method == "POST":
        key = request.form.get("admission_no", "").strip()
        if key:
            start_reset("student", key)
        flash(GENERIC_RESET_MESSAGE, "success")
        return redirect("/student/forgot-password")
    return render_template("forgot_password.html", field_label="Admission Number",
                           field_name="admission_no", placeholder="e.g. DCS-01-0123/2025",
                           action="/student/forgot-password", back_url="/student/login",
                           bar_class="student-bar")


def find_valid_reset(account_type, token):

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""SELECT * FROM password_resets
                      WHERE account_type = %s AND token_hash = %s
                        AND used = 0 AND expires_at > NOW()""",
                   (account_type, token_hash))
    row = cursor.fetchone()
    conn.close()
    return row


@app.route("/reset/<account_type>/<token>", methods=["GET", "POST"])
def reset_password(account_type, token):
    if account_type not in RESET_ACCOUNTS:
        abort(404)
    cfg = RESET_ACCOUNTS[account_type]
    reset = find_valid_reset(account_type, token)
    if not reset:
        return render_template("reset_password.html", valid=False,
                               account_type=account_type, back_url=cfg["login_url"])

    if request.method == "POST":
        new_pw = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(new_pw) < 8:
            flash("Password must be at least 8 characters", "error")
        elif new_pw != confirm:
            flash("Passwords do not match", "error")
        else:
            new_hash = hashlib.sha256(new_pw.encode()).hexdigest()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE {cfg['table']} SET password_hash = %s, failed_attempts = 0, "
                f"locked_until = NULL WHERE {cfg['key_col']} = %s",
                (new_hash, reset["account_key"]))
            cursor.execute("UPDATE password_resets SET used = 1 WHERE reset_id = %s", (reset["reset_id"],))
            conn.commit()
            conn.close()
            flash("Password updated. Please log in with your new password.", "success")
            return redirect(cfg["login_url"])

    return render_template("reset_password.html", valid=True,

                           account_type=account_type, back_url=cfg["login_url"])
load_structures_from_db()

if __name__ == "__main__":
    app.run(debug=True)