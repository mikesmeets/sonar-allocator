import os
import uuid
import random
import hashlib
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'sonar-fleet-secret-key-change-me'

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DATABASE_SQLITE = 'sonar.db'
FLEET_SIZE = 9
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'png', 'jpg', 'jpeg', 'gif'}

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    IntegrityError = psycopg2.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError


# ── database ──────────────────────────────────────────────────────────────────

class _PGConn:
    """Thin wrapper making psycopg2 behave like sqlite3 for our usage."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        if params:
            cur.execute(sql.replace('?', '%s'), params)
        else:
            cur.execute(sql.replace('?', '%s'))
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return _PGConn(conn)
    else:
        conn = sqlite3.connect(DATABASE_SQLITE)
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    db = get_db()
    pk = 'SERIAL PRIMARY KEY' if USE_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS skippers (
            id            {pk},
            name          TEXT NOT NULL,
            email         TEXT UNIQUE NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            is_admin      INTEGER DEFAULT 0,
            was_bumped    INTEGER DEFAULT 0,
            withdrawal_count INTEGER DEFAULT 0
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS races (
            id               {pk},
            race_date        TEXT NOT NULL,
            deadline         TEXT NOT NULL,
            status           TEXT DEFAULT 'open',
            notes            TEXT DEFAULT '',
            available_boats  TEXT DEFAULT '1,2,3,4,5,6,7,8,9'
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS interests (
            id           {pk},
            race_id      INTEGER NOT NULL,
            skipper_id   INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            UNIQUE(race_id, skipper_id)
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS allocations (
            id           {pk},
            race_id      INTEGER NOT NULL,
            skipper_id   INTEGER NOT NULL,
            boat_number  INTEGER NOT NULL,
            UNIQUE(race_id, skipper_id),
            UNIQUE(race_id, boat_number)
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS race_history (
            id           {pk},
            race_id      INTEGER NOT NULL,
            skipper_id   INTEGER NOT NULL,
            boat_number  INTEGER NOT NULL,
            race_date    TEXT NOT NULL
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS allocation_order (
            id            {pk},
            race_id       INTEGER NOT NULL,
            skipper_id    INTEGER NOT NULL,
            priority_rank INTEGER NOT NULL,
            is_late       INTEGER DEFAULT 0,
            is_bumped     INTEGER DEFAULT 0,
            UNIQUE(race_id, skipper_id)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    db.execute(f'''
        CREATE TABLE IF NOT EXISTS documents (
            id                  {pk},
            title               TEXT NOT NULL,
            stored_name         TEXT NOT NULL,
            original_name       TEXT NOT NULL,
            visible_to_skippers INTEGER DEFAULT 0,
            uploaded_at         TEXT NOT NULL
        )
    ''')

    if USE_POSTGRES:
        db.execute("INSERT INTO settings (key, value) VALUES ('deadline_days', '3') ON CONFLICT (key) DO NOTHING")
        db.execute('ALTER TABLE allocation_order ADD COLUMN IF NOT EXISTS is_late INTEGER DEFAULT 0')
        db.execute('ALTER TABLE allocation_order ADD COLUMN IF NOT EXISTS is_bumped INTEGER DEFAULT 0')
        db.execute('ALTER TABLE skippers ADD COLUMN IF NOT EXISTS was_bumped INTEGER DEFAULT 0')
        db.execute('ALTER TABLE skippers ADD COLUMN IF NOT EXISTS withdrawal_count INTEGER DEFAULT 0')
        db.execute("ALTER TABLE races ADD COLUMN IF NOT EXISTS available_boats TEXT DEFAULT '1,2,3,4,5,6,7,8,9'")
    else:
        # SQLite: INSERT OR IGNORE for settings seed
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('deadline_days', '3')")
        # SQLite migrations via PRAGMA
        cols = lambda tbl: [r[1] for r in db.execute(f'PRAGMA table_info({tbl})').fetchall()]
        ao_cols = cols('allocation_order')
        sk_cols = cols('skippers')
        rc_cols = cols('races')
        if 'is_late' not in ao_cols:
            db.execute('ALTER TABLE allocation_order ADD COLUMN is_late INTEGER DEFAULT 0')
        if 'is_bumped' not in ao_cols:
            db.execute('ALTER TABLE allocation_order ADD COLUMN is_bumped INTEGER DEFAULT 0')
        if 'was_bumped' not in sk_cols:
            db.execute('ALTER TABLE skippers ADD COLUMN was_bumped INTEGER DEFAULT 0')
        if 'withdrawal_count' not in sk_cols:
            db.execute('ALTER TABLE skippers ADD COLUMN withdrawal_count INTEGER DEFAULT 0')
        if 'available_boats' not in rc_cols:
            db.execute("ALTER TABLE races ADD COLUMN available_boats TEXT DEFAULT '1,2,3,4,5,6,7,8,9'")

    if not db.execute('SELECT 1 FROM skippers WHERE is_admin=1').fetchone():
        db.execute(
            'INSERT INTO skippers (name, email, password_hash, is_admin) VALUES (?,?,?,1)',
            ('Admin', 'admin@admin.com', hash_pw('admin'))
        )
    db.commit()
    db.close()


def hash_pw(password):
    return hashlib.sha256(password.encode()).hexdigest()


def get_setting(key, default=None):
    db = get_db()
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    db.close()
    return row['value'] if row else default



@app.template_filter('fmt_date')
def fmt_date(s):
    """Format '2026-04-11' as 'Apr 11'."""
    if not s:
        return ''
    try:
        d = datetime.strptime(str(s)[:10], '%Y-%m-%d')
        return d.strftime('%b') + ' ' + str(d.day)
    except ValueError:
        return str(s)


# ── helpers ───────────────────────────────────────────────────────────────────

def count_races_sailed(skipper_id, db):
    return db.execute(
        'SELECT COUNT(*) FROM race_history WHERE skipper_id=?', (skipper_id,)
    ).fetchone()[0]


def get_boat_usage(skipper_id, db):
    """Returns {boat_number: count} for boats 1-9."""
    rows = db.execute(
        'SELECT boat_number, COUNT(*) as cnt FROM race_history '
        'WHERE skipper_id=? GROUP BY boat_number',
        (skipper_id,)
    ).fetchall()
    usage = {i: 0 for i in range(1, FLEET_SIZE + 1)}
    for row in rows:
        usage[row['boat_number']] = row['cnt']
    return usage


def parse_available_boats(available_boats_str):
    """Parse comma-separated boat numbers string into a sorted list of ints."""
    if not available_boats_str:
        return list(range(1, FLEET_SIZE + 1))
    return sorted(int(b) for b in available_boats_str.split(',') if b.strip().isdigit())


def create_priority_list(race_id):
    """
    Build the allocation_order priority list from interested skippers.
    Originals: sorted by sailed ASC, random within tied groups.
    Late joiners (added after a previous prioritisation): by submitted_at ASC.
    Sets race status to 'prioritised'. Does NOT assign boats.
    Returns (success: bool, message: str)
    """
    db = get_db()

    rows = db.execute('''
        SELECT s.id, s.name, s.was_bumped, s.withdrawal_count, i.submitted_at,
               (SELECT COUNT(*) FROM race_history WHERE skipper_id=s.id) AS sailed
        FROM interests i
        JOIN skippers s ON s.id = i.skipper_id
        WHERE i.race_id = ?
    ''', (race_id,)).fetchall()

    if not rows:
        db.close()
        return False, 'No skippers have submitted interest.'

    # Read existing is_late flags so re-prioritising preserves them
    existing = db.execute(
        'SELECT skipper_id, is_late FROM allocation_order WHERE race_id=?', (race_id,)
    ).fetchall()
    original_ids     = {r['skipper_id'] for r in existing if not r['is_late']}
    prev_late_ids    = {r['skipper_id'] for r in existing if r['is_late']}
    all_existing_ids = original_ids | prev_late_ids

    if not all_existing_ids:
        original_rows = [dict(r) for r in rows]
        late_rows     = []
    else:
        original_rows = [dict(r) for r in rows if r['id'] in original_ids]
        late_rows     = [dict(r) for r in rows if r['id'] not in original_ids]

    # Tier 0: was_bumped (priority from being bumped in a previous race)
    # Tier 1: regular (sailed ASC, random tiebreak)
    # Tier 2: deprioritised (withdrawal_count >= 2), sorted last
    bumped_originals  = [r for r in original_rows if r['was_bumped']]
    deprior_originals = [r for r in original_rows if not r['was_bumped'] and r['withdrawal_count'] >= 2]
    regular_originals = [r for r in original_rows if not r['was_bumped'] and r['withdrawal_count'] < 2]

    random.shuffle(bumped_originals)

    def sailed_sorted(rows):
        groups = {}
        for r in rows:
            groups.setdefault(r['sailed'], []).append(r)
        out = []
        for key in sorted(groups):
            g = groups[key]; random.shuffle(g); out.extend(g)
        return out

    sorted_originals = bumped_originals + sailed_sorted(regular_originals) + sailed_sorted(deprior_originals)

    # Late joiners: in the order they signed up (submitted_at ASC)
    sorted_late = sorted(late_rows, key=lambda r: r['submitted_at'])

    sorted_skippers = sorted_originals + sorted_late
    late_ids = {sk['id'] for sk in sorted_late}

    db.execute('DELETE FROM allocation_order WHERE race_id=?', (race_id,))
    for rank, sk in enumerate(sorted_skippers, 1):
        db.execute(
            'INSERT INTO allocation_order (race_id, skipper_id, priority_rank, is_late) VALUES (?,?,?,?)',
            (race_id, sk['id'], rank, 1 if sk['id'] in late_ids else 0)
        )
    db.execute("UPDATE races SET status='prioritised' WHERE id=?", (race_id,))
    db.commit()
    db.close()

    wait_count = len(sorted_skippers) - 1  # everyone's on the list; boats come next
    msg = f'Priority list created for {len(sorted_skippers)} skippers. Assign boats when ready.'
    return True, msg


def assign_boats(race_id):
    """
    Full reallocation: top N skippers by priority_rank get boats (N = available boats).
    Boat-to-skipper assignment minimises repeat boats (fewest prior uses, random tie-break).
    Clears is_bumped for anyone who receives a boat.
    Sets race status to 'allocated'. Priority list must already exist.
    Returns (success: bool, message: str)
    """
    db = get_db()

    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()
    available_boats = parse_available_boats(race['available_boats'])

    priority_ids = [r['skipper_id'] for r in db.execute(
        'SELECT skipper_id FROM allocation_order WHERE race_id=? ORDER BY priority_rank ASC',
        (race_id,)
    ).fetchall()]

    if not priority_ids:
        db.close()
        return False, 'No priority list found — prioritise the race first.'

    getting_boats = priority_ids[:len(available_boats)]

    remaining = list(available_boats)
    assignments = {}
    for sid in getting_boats:
        usage = get_boat_usage(sid, db)
        opts  = list(remaining)
        random.shuffle(opts)
        opts.sort(key=lambda b: usage[b])
        boat = opts[0]
        assignments[sid] = boat
        remaining.remove(boat)

    db.execute('DELETE FROM allocations WHERE race_id=?', (race_id,))
    for sid, boat in assignments.items():
        db.execute(
            'INSERT INTO allocations (race_id, skipper_id, boat_number) VALUES (?,?,?)',
            (race_id, sid, boat)
        )
        db.execute(
            'UPDATE allocation_order SET is_bumped=0 WHERE race_id=? AND skipper_id=?',
            (race_id, sid)
        )
        # Clear the skipper-level bumped flag only if there was a waitlist
        # (i.e. priority actually mattered — more skippers than boats)
        if len(priority_ids) > len(available_boats):
            db.execute('UPDATE skippers SET was_bumped=0 WHERE id=?', (sid,))
    db.execute("UPDATE races SET status='allocated' WHERE id=?", (race_id,))

    db.commit()
    db.close()

    waitlisted = len(priority_ids) - len(assignments)
    msg = f'Boats assigned to {len(assignments)} skippers.'
    if waitlisted > 0:
        msg += f' {waitlisted} on the waitlist.'
    return True, msg


# ── auth decorators ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'skipper_id' not in session:
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Admin access required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ── public routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'skipper_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('admin') if session.get('is_admin') else url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = request.form.get('password', '')
        db = get_db()
        sk = db.execute('SELECT * FROM skippers WHERE LOWER(email)=?', (email,)).fetchone()
        db.close()
        if sk:
            if hash_pw(password) != sk['password_hash']:
                flash('Invalid password.', 'danger')
                return render_template('login.html')
            session.clear()
            session['skipper_id']   = sk['id']
            session['skipper_name'] = sk['name']
            session['is_admin']     = bool(sk['is_admin'])
            return redirect(url_for('admin') if sk['is_admin'] else url_for('dashboard'))
        flash('Email not recognised.', 'danger')
    return render_template('login.html', show_signup=False)


@app.route('/signup', methods=['POST'])
def signup():
    name             = request.form['name'].strip()
    email            = request.form['email'].strip().lower()
    password         = request.form['password']
    confirm_password = request.form['confirm_password']

    if not name or not email or not password:
        flash('All fields are required.', 'danger')
        return render_template('login.html', show_signup=True)

    if password != confirm_password:
        flash('Passwords do not match.', 'danger')
        return render_template('login.html', show_signup=True)

    db = get_db()
    try:
        db.execute(
            'INSERT INTO skippers (name, email, password_hash) VALUES (?,?,?)',
            (name, email, hash_pw(password))
        )
        db.commit()
        sk = db.execute('SELECT * FROM skippers WHERE LOWER(email)=?', (email,)).fetchone()
        session.clear()
        session['skipper_id']   = sk['id']
        session['skipper_name'] = sk['name']
        session['is_admin']     = False
        flash(f'Welcome, {name}! Your account has been created.', 'success')
        db.close()
        return redirect(url_for('dashboard'))
    except IntegrityError:
        db.rollback()
        flash('That email is already registered.', 'danger')
        db.close()
        return render_template('login.html', show_signup=True)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── skipper routes ────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    skipper = db.execute('SELECT * FROM skippers WHERE id=?', (session['skipper_id'],)).fetchone()
    now     = datetime.now().isoformat()

    races = db.execute(
        "SELECT * FROM races WHERE status != 'completed' ORDER BY race_date ASC"
    ).fetchall()

    race_data = []
    for race in races:
        submitted = db.execute(
            'SELECT 1 FROM interests WHERE race_id=? AND skipper_id=?',
            (race['id'], session['skipper_id'])
        ).fetchone() is not None

        alloc = db.execute(
            'SELECT boat_number FROM allocations WHERE race_id=? AND skipper_id=?',
            (race['id'], session['skipper_id'])
        ).fetchone()

        alloc_count = db.execute(
            'SELECT COUNT(*) FROM allocations WHERE race_id=?', (race['id'],)
        ).fetchone()[0]
        spare_boats = len(parse_available_boats(race['available_boats'])) - alloc_count

        priority_row = db.execute(
            'SELECT priority_rank FROM allocation_order WHERE race_id=? AND skipper_id=?',
            (race['id'], session['skipper_id'])
        ).fetchone()

        waitlist_position = None
        if priority_row and not alloc:
            ahead = db.execute('''
                SELECT COUNT(*) FROM allocation_order ao
                WHERE ao.race_id = ?
                  AND ao.priority_rank < ?
                  AND ao.skipper_id NOT IN (SELECT skipper_id FROM allocations WHERE race_id = ?)
            ''', (race['id'], priority_row['priority_rank'], race['id'])).fetchone()[0]
            waitlist_position = ahead + 1

        race_data.append({
            'race':             race,
            'submitted':        submitted,
            'boat':             alloc['boat_number'] if alloc else None,
            'past_deadline':    now > race['deadline'],
            'spare_boats':      spare_boats,
            'priority_rank':    priority_row['priority_rank'] if priority_row else None,
            'waitlist_position': waitlist_position,
        })

    sailed = count_races_sailed(session['skipper_id'], db)

    completed_races = db.execute(
        "SELECT * FROM races WHERE status = 'completed' ORDER BY race_date DESC"
    ).fetchall()
    completed_data = []
    for race in completed_races:
        history = db.execute(
            'SELECT boat_number FROM race_history WHERE race_id=? AND skipper_id=?',
            (race['id'], session['skipper_id'])
        ).fetchone()
        completed_data.append({
            'race': race,
            'boat': history['boat_number'] if history else None,
        })

    documents = db.execute(
        'SELECT * FROM documents WHERE visible_to_skippers=1 ORDER BY uploaded_at DESC'
    ).fetchall()
    notice_text = get_setting('notice_text', '')
    db.close()

    return render_template('dashboard.html',
                           skipper=skipper,
                           race_data=race_data,
                           sailed=sailed,
                           completed_data=completed_data,
                           documents=documents,
                           notice_text=notice_text)


@app.route('/interest/<int:race_id>/submit', methods=['POST'])
@login_required
def submit_interest(race_id):
    db = get_db()
    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()
    if not race:
        flash('Raceday not found.', 'danger')
    elif race['status'] != 'open':
        flash('Submissions are not open for this raceday.', 'danger')
    elif datetime.now().isoformat() > race['deadline']:
        flash('The submission deadline has passed.', 'danger')
    else:
        try:
            db.execute(
                'INSERT INTO interests (race_id, skipper_id, submitted_at) VALUES (?,?,?)',
                (race_id, session['skipper_id'], datetime.now().isoformat())
            )
            db.commit()
            flash("You're in — interest submitted!", 'success')
        except IntegrityError:
            db.rollback()
            flash('Already submitted for this raceday.', 'warning')
    db.close()
    return redirect(url_for('dashboard'))


@app.route('/interest/<int:race_id>/withdraw', methods=['POST'])
@login_required
def withdraw_interest(race_id):
    db = get_db()
    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()
    if race and datetime.now().isoformat() <= race['deadline']:
        db.execute(
            'DELETE FROM interests WHERE race_id=? AND skipper_id=?',
            (race_id, session['skipper_id'])
        )
        db.commit()
        flash('Interest withdrawn.', 'info')
    else:
        flash('The deadline has passed; you cannot withdraw.', 'danger')
    db.close()
    return redirect(url_for('dashboard'))


@app.route('/interest/<int:race_id>/join_late', methods=['POST'])
@login_required
def join_late(race_id):
    db = get_db()
    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()

    if not race or race['status'] not in ('prioritised', 'allocated'):
        flash('This raceday is not open for late joining.', 'danger')
        db.close()
        return redirect(url_for('dashboard'))

    already = db.execute(
        'SELECT 1 FROM interests WHERE race_id=? AND skipper_id=?',
        (race_id, session['skipper_id'])
    ).fetchone()
    if already:
        flash('You have already joined this raceday.', 'warning')
        db.close()
        return redirect(url_for('dashboard'))

    now = datetime.now().isoformat()
    db.execute('INSERT INTO interests (race_id, skipper_id, submitted_at) VALUES (?,?,?)',
               (race_id, session['skipper_id'], now))

    max_rank = db.execute(
        'SELECT MAX(priority_rank) FROM allocation_order WHERE race_id=?', (race_id,)
    ).fetchone()[0] or 0
    db.execute(
        'INSERT INTO allocation_order (race_id, skipper_id, priority_rank, is_late) VALUES (?,?,?,1)',
        (race_id, session['skipper_id'], max_rank + 1)
    )

    boat_assigned = None
    if race['status'] == 'allocated':
        available_boats = parse_available_boats(race['available_boats'])
        allocated_boats = {r['boat_number'] for r in db.execute(
            'SELECT boat_number FROM allocations WHERE race_id=?', (race_id,)
        ).fetchall()}
        spare = [b for b in available_boats if b not in allocated_boats]
        if spare:
            usage = get_boat_usage(session['skipper_id'], db)
            random.shuffle(spare)
            spare.sort(key=lambda b: usage[b])
            boat_assigned = spare[0]
            db.execute(
                'INSERT INTO allocations (race_id, skipper_id, boat_number) VALUES (?,?,?)',
                (race_id, session['skipper_id'], boat_assigned)
            )

    db.commit()
    db.close()

    if boat_assigned:
        flash(f'Joined — you have been assigned Boat #{boat_assigned}.', 'success')
    else:
        flash('Joined — you are on the waitlist.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/interest/<int:race_id>/withdraw_allocation', methods=['POST'])
@login_required
def withdraw_allocation(race_id):
    db = get_db()
    race  = db.execute('SELECT status FROM races WHERE id=?', (race_id,)).fetchone()
    alloc = db.execute(
        'SELECT boat_number FROM allocations WHERE race_id=? AND skipper_id=?',
        (race_id, session['skipper_id'])
    ).fetchone()

    if not race or race['status'] != 'allocated' or not alloc:
        flash('Cannot withdraw from this raceday.', 'danger')
        db.close()
        return redirect(url_for('dashboard'))

    db.execute('DELETE FROM interests      WHERE race_id=? AND skipper_id=?', (race_id, session['skipper_id']))
    db.execute('DELETE FROM allocations    WHERE race_id=? AND skipper_id=?', (race_id, session['skipper_id']))
    db.execute('DELETE FROM allocation_order WHERE race_id=? AND skipper_id=?', (race_id, session['skipper_id']))
    db.execute('UPDATE skippers SET withdrawal_count = withdrawal_count + 1 WHERE id=?', (session['skipper_id'],))
    db.commit()

    new_count = db.execute(
        'SELECT withdrawal_count FROM skippers WHERE id=?', (session['skipper_id'],)
    ).fetchone()[0]
    db.close()

    if new_count >= 2:
        flash('Withdrawal recorded. You have now withdrawn twice — you will be deprioritised in future allocations.', 'warning')
    else:
        flash('Withdrawal recorded. Note: a second withdrawal will result in deprioritisation in future allocations.', 'warning')
    return redirect(url_for('dashboard'))


# ── admin routes ──────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin():
    db = get_db()
    races = db.execute('SELECT * FROM races ORDER BY race_date DESC').fetchall()
    race_data = []
    for race in races:
        ni = db.execute('SELECT COUNT(*) FROM interests   WHERE race_id=?', (race['id'],)).fetchone()[0]
        na = db.execute('SELECT COUNT(*) FROM allocations WHERE race_id=?', (race['id'],)).fetchone()[0]
        spare = len(parse_available_boats(race['available_boats'])) - na
        # needs_rerun: allocated but priority list has unassigned skippers with spare boats available
        needs_rerun = race['status'] == 'allocated' and ni > na and spare > 0
        race_data.append({'race': race, 'interest_count': ni, 'alloc_count': na, 'needs_rerun': needs_rerun, 'spare': spare})

    skippers = db.execute('''
        SELECT s.id, s.name, s.email, s.withdrawal_count,
               (SELECT COUNT(*) FROM race_history WHERE skipper_id=s.id) AS sailed
        FROM skippers s
        WHERE s.is_admin = 0
        ORDER BY s.name
    ''').fetchall()
    deadline_days   = int(get_setting('deadline_days', 3))
    notice_text     = get_setting('notice_text', '')
    documents = db.execute('SELECT * FROM documents ORDER BY uploaded_at DESC').fetchall()
    db.close()
    return render_template('admin.html', race_data=race_data, skippers=skippers,
                           deadline_days=deadline_days, documents=documents,
                           notice_text=notice_text)


@app.route('/admin/race/create', methods=['POST'])
@admin_required
def create_race():
    race_date_strs  = request.form.getlist('race_dates')
    notes           = request.form.get('notes', '').strip()
    selected_boats  = request.form.getlist('boats')
    if not race_date_strs:
        flash('Please select at least one date.', 'danger')
        return redirect(url_for('admin'))
    if not selected_boats:
        flash('Please select at least one boat.', 'danger')
        return redirect(url_for('admin'))
    available_boats = ','.join(sorted(selected_boats, key=int))
    deadline_days   = int(get_setting('deadline_days', 3))
    status          = 'open' if request.form.get('open_now') else 'scheduled'
    db = get_db()
    created = 0
    for race_date_str in sorted(set(race_date_strs)):
        race_date   = datetime.strptime(race_date_str, '%Y-%m-%d')
        deadline_dt = (race_date - timedelta(days=deadline_days)).replace(hour=23, minute=59, second=59)
        db.execute(
            'INSERT INTO races (race_date, deadline, notes, available_boats, status) VALUES (?,?,?,?,?)',
            (race_date_str, deadline_dt.isoformat(), notes, available_boats, status)
        )
        created += 1
    db.commit()
    db.close()
    label = f'{created} raceday{"s" if created != 1 else ""}'
    flash(f'{label} created ({status}).', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/race/<int:race_id>/delete', methods=['POST'])
@admin_required
def delete_race(race_id):
    db = get_db()
    db.execute('DELETE FROM race_history WHERE race_id=?', (race_id,))
    db.execute('DELETE FROM allocations   WHERE race_id=?', (race_id,))
    db.execute('DELETE FROM interests     WHERE race_id=?', (race_id,))
    db.execute('DELETE FROM races         WHERE id=?',      (race_id,))
    db.commit()
    db.close()
    flash('Raceday deleted.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/race/<int:race_id>')
@admin_required
def race_detail(race_id):
    db = get_db()
    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()
    if not race:
        flash('Raceday not found.', 'danger')
        return redirect(url_for('admin'))

    interested = db.execute('''
        SELECT s.id, s.name, i.submitted_at,
               (SELECT COUNT(*) FROM race_history WHERE skipper_id=s.id) AS sailed
        FROM interests i
        JOIN skippers s ON s.id = i.skipper_id
        WHERE i.race_id = ?
        ORDER BY sailed ASC, i.submitted_at ASC
    ''', (race_id,)).fetchall()

    allocations = db.execute('''
        SELECT a.skipper_id, a.boat_number, s.name
        FROM allocations a
        JOIN skippers s ON s.id = a.skipper_id
        WHERE a.race_id = ?
        ORDER BY a.boat_number
    ''', (race_id,)).fetchall()

    not_submitted = db.execute('''
        SELECT id, name FROM skippers
        WHERE is_admin = 0
          AND id NOT IN (SELECT skipper_id FROM interests WHERE race_id=?)
        ORDER BY name
    ''', (race_id,)).fetchall()

    allocated_ids   = {a['skipper_id'] for a in allocations}
    waitlisted      = [sk for sk in interested if sk['id'] not in allocated_ids]
    available_boats = parse_available_boats(race['available_boats'])

    boat_assignments = {a['boat_number']: a['skipper_id'] for a in allocations}

    priority_list = db.execute('''
        SELECT ao.priority_rank, ao.is_late, ao.is_bumped, s.id, s.name,
               (SELECT COUNT(*) FROM race_history WHERE skipper_id=s.id) AS sailed,
               a.boat_number
        FROM allocation_order ao
        JOIN skippers s ON s.id = ao.skipper_id
        LEFT JOIN allocations a ON a.race_id = ao.race_id AND a.skipper_id = ao.skipper_id
        WHERE ao.race_id = ?
        ORDER BY ao.priority_rank ASC
    ''', (race_id,)).fetchall()

    bumped_ids = {r['skipper_id'] for r in db.execute(
        'SELECT skipper_id FROM allocation_order WHERE race_id=? AND is_bumped=1', (race_id,)
    ).fetchall()}

    db.close()
    return render_template('race_detail.html',
                           race=race,
                           interested=interested,
                           allocations=allocations,
                           not_submitted=not_submitted,
                           waitlisted=waitlisted,
                           available_boats=available_boats,
                           priority_list=priority_list,
                           boat_assignments=boat_assignments,
                           bumped_ids=bumped_ids)


@app.route('/admin/race/<int:race_id>/set_status', methods=['POST'])
@admin_required
def set_race_status(race_id):
    new_status = request.form['status']
    if new_status not in ('scheduled', 'open'):
        flash('Invalid status.', 'danger')
        return redirect(url_for('race_detail', race_id=race_id))
    db = get_db()
    if new_status == 'open':
        # Clear priority list when re-opening so it gets rebuilt fresh
        db.execute('DELETE FROM allocation_order WHERE race_id=?', (race_id,))
    db.execute('UPDATE races SET status=? WHERE id=?', (new_status, race_id))
    db.commit()
    db.close()

    label = 'opened for submissions' if new_status == 'open' else 'closed for submissions'
    flash(f'Raceday {label}.', 'success')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/update_dates', methods=['POST'])
@admin_required
def update_race_dates(race_id):
    race_date_str = request.form['race_date']
    deadline_str  = request.form['deadline_date']
    try:
        race_date    = datetime.strptime(race_date_str, '%Y-%m-%d')
        deadline_dt  = datetime.strptime(deadline_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    except ValueError:
        flash('Invalid date format.', 'danger')
        return redirect(url_for('race_detail', race_id=race_id))
    db = get_db()
    db.execute('UPDATE races SET race_date=?, deadline=? WHERE id=?',
               (race_date_str, deadline_dt.isoformat(), race_id))
    db.commit()
    db.close()
    flash('Dates updated.', 'success')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/update_boats', methods=['POST'])
@admin_required
def update_race_boats(race_id):
    selected = request.form.getlist('boats')
    if not selected:
        flash('Select at least one boat.', 'danger')
        return redirect(url_for('race_detail', race_id=race_id))

    new_boats_str = ','.join(sorted(selected, key=int))
    db = get_db()
    race = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()

    if race['status'] == 'allocated':
        old_boats = parse_available_boats(race['available_boats'])
        new_n, old_n = len(selected), len(old_boats)

        # Determine which skippers fall outside / inside the new cutoff
        priority_ids = [r['skipper_id'] for r in db.execute(
            'SELECT skipper_id FROM allocation_order WHERE race_id=? ORDER BY priority_rank ASC',
            (race_id,)
        ).fetchall()]

        newly_bumped   = set(priority_ids[new_n:old_n]) if new_n < old_n else set()
        newly_promoted = set(priority_ids[old_n:new_n]) if new_n > old_n else set()

        for sid in newly_bumped:
            db.execute(
                'UPDATE allocation_order SET is_bumped=1 WHERE race_id=? AND skipper_id=?',
                (race_id, sid)
            )
            db.execute('UPDATE skippers SET was_bumped=1 WHERE id=?', (sid,))

        db.execute('UPDATE races SET available_boats=? WHERE id=?', (new_boats_str, race_id))
        db.commit()
        db.close()

        assign_boats(race_id)   # full reallocation; clears is_bumped for those getting boats

        parts = []
        if newly_bumped:
            n = len(newly_bumped)
            parts.append(f'{n} skipper{"s" if n != 1 else ""} bumped to waitlist.')
        if newly_promoted:
            n = len(newly_promoted)
            parts.append(f'{n} skipper{"s" if n != 1 else ""} promoted from waitlist.')
        flash(
            ' '.join(parts) if parts else 'Available boats updated — boats re-allocated.',
            'warning' if newly_bumped else 'success'
        )
    else:
        db.execute(
            "UPDATE races SET available_boats=? WHERE id=? AND status IN ('scheduled','open','prioritised')",
            (new_boats_str, race_id)
        )
        db.commit()
        db.close()
        flash('Available boats updated.', 'success')

    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/update_notes', methods=['POST'])
@admin_required
def update_race_notes(race_id):
    notes = request.form.get('notes', '').strip()
    db = get_db()
    db.execute('UPDATE races SET notes=? WHERE id=?', (notes, race_id))
    db.commit()
    db.close()
    flash('Notes updated.', 'success')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/add_interest', methods=['POST'])
@admin_required
def admin_add_interest(race_id):
    skipper_id = int(request.form['skipper_id'])
    db = get_db()
    race = db.execute('SELECT status FROM races WHERE id=?', (race_id,)).fetchone()
    try:
        now = datetime.now().isoformat()
        db.execute(
            'INSERT INTO interests (race_id, skipper_id, submitted_at) VALUES (?,?,?)',
            (race_id, skipper_id, now)
        )
        # If a priority list already exists, append this skipper as a late joiner
        if race['status'] in ('prioritised', 'allocated'):
            max_rank = db.execute(
                'SELECT MAX(priority_rank) FROM allocation_order WHERE race_id=?', (race_id,)
            ).fetchone()[0] or 0
            db.execute(
                'INSERT INTO allocation_order (race_id, skipper_id, priority_rank, is_late) VALUES (?,?,?,1)',
                (race_id, skipper_id, max_rank + 1)
            )
        db.commit()
        flash('Skipper added to the priority list.', 'success')
    except IntegrityError:
        db.rollback()
        flash('Skipper already submitted.', 'warning')
    db.close()
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/remove_interest/<int:skipper_id>', methods=['POST'])
@admin_required
def admin_remove_interest(race_id, skipper_id):
    db = get_db()
    db.execute('DELETE FROM interests        WHERE race_id=? AND skipper_id=?', (race_id, skipper_id))
    db.execute('DELETE FROM allocations      WHERE race_id=? AND skipper_id=?', (race_id, skipper_id))
    db.execute('DELETE FROM allocation_order WHERE race_id=? AND skipper_id=?', (race_id, skipper_id))
    db.commit()
    db.close()
    flash('Skipper removed.', 'info')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/prioritise', methods=['POST'])
@admin_required
def prioritise_race(race_id):
    ok, msg = create_priority_list(race_id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/allocate', methods=['POST'])
@admin_required
def allocate_race(race_id):
    ok, msg = assign_boats(race_id)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/set_boat', methods=['POST'])
@admin_required
def set_boat(race_id):
    skipper_id  = int(request.form['skipper_id'])
    boat_number = request.form.get('boat_number', '').strip()
    db = get_db()
    if not boat_number:
        db.execute('DELETE FROM allocations WHERE race_id=? AND skipper_id=?', (race_id, skipper_id))
        db.commit()
        flash('Boat assignment cleared.', 'info')
    else:
        boat_number = int(boat_number)
        existing = db.execute(
            'SELECT skipper_id FROM allocations WHERE race_id=? AND boat_number=?',
            (race_id, boat_number)
        ).fetchone()
        if existing and existing['skipper_id'] != skipper_id:
            flash(f'Boat {boat_number} is already assigned to another skipper.', 'danger')
        else:
            db.execute(
                '''INSERT INTO allocations (race_id, skipper_id, boat_number) VALUES (?,?,?)
                   ON CONFLICT (race_id, skipper_id) DO UPDATE SET boat_number = EXCLUDED.boat_number''',
                (race_id, skipper_id, boat_number)
            )
            db.commit()
            flash(f'Boat {boat_number} assigned.', 'success')
    db.close()
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/unallocate', methods=['POST'])
@admin_required
def unallocate_race(race_id):
    db = get_db()
    # Clear boat assignments but keep the priority list intact
    db.execute('DELETE FROM allocations WHERE race_id=?', (race_id,))
    # Clear any bumped flags since there are no allocations to be bumped from
    db.execute('UPDATE allocation_order SET is_bumped=0 WHERE race_id=?', (race_id,))
    db.execute("UPDATE races SET status='prioritised' WHERE id=? AND status='allocated'", (race_id,))
    db.commit()
    db.close()
    flash('Boat assignments cleared — priority list preserved.', 'info')
    return redirect(url_for('race_detail', race_id=race_id))


@app.route('/admin/race/<int:race_id>/complete', methods=['POST'])
@admin_required
def complete_race(race_id):
    db   = get_db()
    race  = db.execute('SELECT * FROM races WHERE id=?', (race_id,)).fetchone()
    allocs = db.execute('SELECT * FROM allocations WHERE race_id=?', (race_id,)).fetchall()

    if not allocs:
        flash('No allocations to record — run allocation first.', 'danger')
        db.close()
        return redirect(url_for('race_detail', race_id=race_id))

    for a in allocs:
        db.execute(
            'INSERT INTO race_history (race_id, skipper_id, boat_number, race_date) '
            'VALUES (?,?,?,?)',
            (race_id, a['skipper_id'], a['boat_number'], race['race_date'])
        )
    db.execute("UPDATE races SET status='completed' WHERE id=?", (race_id,))
    db.commit()
    db.close()
    flash('Raceday completed — history updated.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/skipper/add', methods=['POST'])
@admin_required
def add_skipper():
    name     = request.form['name'].strip()
    email    = request.form['email'].strip().lower()
    password = request.form['password']
    is_admin = 1 if request.form.get('is_admin') else 0
    db = get_db()
    try:
        db.execute(
            'INSERT INTO skippers (name, email, password_hash, is_admin) VALUES (?,?,?,?)',
            (name, email, hash_pw(password), is_admin)
        )
        db.commit()
        role = 'Admin' if is_admin else 'Skipper'
        flash(f'{role} {name} added.', 'success')
    except IntegrityError:
        db.rollback()
        flash('That email is already registered.', 'danger')
    db.close()
    return redirect(url_for('admin'))


@app.route('/admin/skipper/<int:skipper_id>/reset_password', methods=['POST'])
@admin_required
def reset_skipper_password(skipper_id):
    new_pw = request.form['new_password']
    db = get_db()
    db.execute('UPDATE skippers SET password_hash=? WHERE id=?', (hash_pw(new_pw), skipper_id))
    db.commit()
    db.close()
    flash('Password reset.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/skipper/<int:skipper_id>/edit', methods=['POST'])
@admin_required
def edit_skipper(skipper_id):
    name     = request.form['name'].strip()
    email    = request.form['email'].strip().lower()
    new_pw   = request.form.get('new_password', '').strip()
    db = get_db()
    try:
        db.execute(
            'UPDATE skippers SET name=?, email=? WHERE id=? AND is_admin=0',
            (name, email, skipper_id)
        )
        if new_pw:
            db.execute('UPDATE skippers SET password_hash=? WHERE id=?',
                       (hash_pw(new_pw), skipper_id))
        db.commit()
        flash(f'{name} updated.', 'success')
    except IntegrityError:
        db.rollback()
        flash('That email is already registered to another skipper.', 'danger')
    db.close()
    return redirect(url_for('admin'))


@app.route('/admin/skipper/<int:skipper_id>/remove', methods=['POST'])
@admin_required
def remove_skipper(skipper_id):
    db = get_db()
    db.execute('DELETE FROM skippers  WHERE id=? AND is_admin=0', (skipper_id,))
    db.execute('DELETE FROM interests WHERE skipper_id=?', (skipper_id,))
    db.commit()
    db.close()
    flash('Skipper removed.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/change_password', methods=['POST'])
@admin_required
def change_admin_password():
    current_pw = request.form['current_password']
    new_pw     = request.form['new_password']
    db = get_db()
    admin = db.execute(
        'SELECT * FROM skippers WHERE id=? AND is_admin=1', (session['skipper_id'],)
    ).fetchone()
    if admin and hash_pw(current_pw) == admin['password_hash']:
        db.execute('UPDATE skippers SET password_hash=? WHERE id=?',
                   (hash_pw(new_pw), session['skipper_id']))
        db.commit()
        flash('Password updated.', 'success')
    else:
        flash('Current password incorrect.', 'danger')
    db.close()
    return redirect(url_for('admin'))


@app.route('/admin/settings', methods=['POST'])
@admin_required
def save_settings():
    deadline_days = request.form.get('deadline_days', '').strip()
    if not deadline_days.isdigit() or int(deadline_days) < 1:
        flash('Deadline must be a positive number of days.', 'danger')
        return redirect(url_for('admin'))
    notice_text = request.form.get('notice_text', '')
    db = get_db()
    upsert = ("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
              if USE_POSTGRES else
              "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)")
    db.execute(upsert, ('deadline_days', deadline_days))
    db.execute(upsert, ('notice_text', notice_text))
    db.commit()
    db.close()
    flash('Settings saved.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/documents/upload', methods=['POST'])
@admin_required
def upload_document():
    f = request.files.get('file')
    if not f or f.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('admin'))
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        flash(f'File type .{ext} not allowed.', 'danger')
        return redirect(url_for('admin'))
    original_name = secure_filename(f.filename)
    stored_name   = f'{uuid.uuid4().hex}.{ext}'
    title         = request.form.get('title', '').strip() or original_name
    f.save(os.path.join(UPLOAD_FOLDER, stored_name))
    visible = 1 if request.form.get('visible') else 0
    db = get_db()
    db.execute(
        'INSERT INTO documents (title, stored_name, original_name, visible_to_skippers, uploaded_at) VALUES (?,?,?,?,?)',
        (title, stored_name, original_name, visible, datetime.now().isoformat())
    )
    db.commit()
    db.close()
    flash(f'"{title}" uploaded.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/documents/<int:doc_id>/update', methods=['POST'])
@admin_required
def update_document(doc_id):
    title   = request.form.get('title', '').strip()
    visible = 1 if request.form.get('visible') else 0
    db = get_db()
    db.execute('UPDATE documents SET title=?, visible_to_skippers=? WHERE id=?',
               (title, visible, doc_id))
    db.commit()
    db.close()
    flash('Document updated.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/documents/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_document(doc_id):
    db = get_db()
    row = db.execute('SELECT stored_name, title FROM documents WHERE id=?', (doc_id,)).fetchone()
    if row:
        db.execute('DELETE FROM documents WHERE id=?', (doc_id,))
        db.commit()
        path = os.path.join(UPLOAD_FOLDER, row['stored_name'])
        if os.path.exists(path):
            os.remove(path)
        flash(f'"{row["title"]}" deleted.', 'info')
    db.close()
    return redirect(url_for('admin'))


@app.route('/documents/<int:doc_id>')
@login_required
def download_document(doc_id):
    db = get_db()
    row = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
    db.close()
    if not row:
        flash('Document not found.', 'danger')
        return redirect(url_for('dashboard'))
    if not session.get('is_admin') and not row['visible_to_skippers']:
        flash('Document not available.', 'danger')
        return redirect(url_for('dashboard'))
    return send_from_directory(UPLOAD_FOLDER, row['stored_name'],
                               as_attachment=False,
                               download_name=row['original_name'])


@app.route('/admin/stats')
@admin_required
def stats():
    db = get_db()
    skippers = db.execute(
        'SELECT id, name FROM skippers WHERE is_admin=0 ORDER BY name'
    ).fetchall()
    matrix = {sk['id']: get_boat_usage(sk['id'], db) for sk in skippers}
    db.close()
    return render_template('stats.html', skippers=skippers,
                           matrix=matrix, fleet_size=FLEET_SIZE)


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 4000))
    print(f'\n  Sonar Crew Allocator running at http://localhost:{port}')
    print('  Default login: admin / admin\n')
    app.run(debug=False, host='0.0.0.0', port=port)
