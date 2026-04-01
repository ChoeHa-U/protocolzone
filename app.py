import hashlib
import time
from datetime import date, datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for

import database as db

app = Flask(__name__)
# Flask uses this key to safely sign session data, like the logged-in user id.
app.secret_key = 'dev-secret-key'

DATE_FORMAT = '%Y-%m-%d'
MAX_TRACKERS = 3
TRACKER_TYPES = ('subject', 'habit', 'goal')
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 20
MIN_PASSWORD_LENGTH = 6
MIN_GOAL_DAYS = 1
MAX_GOAL_DAYS = 254
DEFAULT_GOAL_DAYS = 21
CALENDAR_GRID_DAY_COUNT = 42
CHECKIN_PROMPT_COOLDOWN_SECONDS = 5 * 60
# Change this value if you want the popup to wait longer or shorter before showing again.

# Build the database tables when the app starts.
db.init_db()


def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def password_matches(password, saved_password_hash):
    return hash_password(password) == saved_password_hash


def get_logged_in_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.get_user_by_id(user_id)


def parse_int(text_value, fallback=None):
    try:
        return int(text_value)
    except (TypeError, ValueError):
        return fallback


def parse_goal_days(goal_days_text):
    goal_days = parse_int(goal_days_text)
    if goal_days is None:
        return None
    if MIN_GOAL_DAYS <= goal_days <= MAX_GOAL_DAYS:
        return goal_days
    return None


def parse_iso_date(date_text):
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, DATE_FORMAT).date()
    except ValueError:
        return None


def add_tracker_defaults(tracker):
    tracker.setdefault('goal_days', DEFAULT_GOAL_DAYS)
    tracker.setdefault('quote', '')
    tracker.setdefault('checkin_lookup', {})
    tracker.setdefault('current_streak', 0)
    tracker.setdefault('calendar', [])
    tracker.setdefault('question_text', '')
    tracker.setdefault('display_quote', '')
    tracker.setdefault('headline_text', '')
    tracker.setdefault('progress_pct', 0)
    tracker.setdefault('streak_unit', 'days')


def build_checkin_lookup(checkin_rows):
    checkin_lookup = {}
    for checkin_row in checkin_rows:
        if checkin_row['status'] == 'clean':
            checkin_lookup[checkin_row['date']] = True
        elif checkin_row['status'] == 'slipped':
            checkin_lookup[checkin_row['date']] = False
        else:
            checkin_lookup[checkin_row['date']] = None
    return checkin_lookup


def update_streak_totals(tracker):
    current_streak = 0

    for day_text in sorted(tracker['checkin_lookup']):
        day_result = tracker['checkin_lookup'][day_text]
        if day_result is True:
            current_streak += 1
        elif day_result is False:
            current_streak = 0

    tracker['current_streak'] = current_streak
    tracker['streak_unit'] = 'day' if current_streak in (0, 1) else 'days'


def shift_month_start(base_month_start, month_offset):
    month_number = (base_month_start.year * 12 + (base_month_start.month - 1)) + month_offset
    target_year = month_number // 12
    target_month = (month_number % 12) + 1
    return date(target_year, target_month, 1)


def build_calendar_cells(tracker, month_offset=0):
    today = date.today()
    tracker_start_date = parse_iso_date(tracker.get('start_date')) or today
    can_open_checkin = tracker['type'] in ('habit', 'goal')
    month_start = shift_month_start(today.replace(day=1), month_offset)
    calendar_start = month_start - timedelta(days=month_start.weekday())
    calendar_cells = []

    for day_offset in range(CALENDAR_GRID_DAY_COUNT):
        current_day = calendar_start + timedelta(days=day_offset)
        day_text = current_day.isoformat()
        is_inside_month = (
            current_day.year == month_start.year
            and current_day.month == month_start.month
        )
        is_future_day = current_day > today
        checkin_state = tracker['checkin_lookup'].get(day_text)

        if not is_inside_month:
            cell_style = 'outside'
            is_clickable = False
        elif is_future_day or current_day < tracker_start_date:
            cell_style = 'pending'
            is_clickable = False
        elif checkin_state is True:
            cell_style = 'done'
            is_clickable = can_open_checkin
        elif checkin_state is False:
            cell_style = 'miss'
            is_clickable = can_open_checkin
        else:
            cell_style = 'pending'
            is_clickable = can_open_checkin

        calendar_cells.append(
            {
                'date': day_text,
                'day': current_day.strftime('%d'),
                'style': cell_style,
                'clickable': is_clickable,
                'in_month': is_inside_month,
            }
        )

    tracker['calendar'] = calendar_cells
    tracker['month_label'] = month_start.strftime('%B %Y')


def prepare_tracker_for_dashboard(tracker, month_offset=0):
    """
    The database stores only the raw tracker row and its check-in logs.
    This helper builds the extra values the dashboard needs to display.
    """
    add_tracker_defaults(tracker)
    tracker['checkin_lookup'] = build_checkin_lookup(db.get_checkin_logs(tracker['id']))
    update_streak_totals(tracker)
    build_calendar_cells(tracker, month_offset=month_offset)

    if tracker['type'] == 'habit':
        tracker['question_text'] = 'Did you avoid the habit?'
        tracker['headline_text'] = f"You been {tracker['name']} free for :"
    else:
        tracker['question_text'] = 'Did you make progress on this today?'
        tracker['headline_text'] = f"You been focusing on {tracker['name']} for:"

    if tracker['goal_days']:
        progress_ratio = tracker['current_streak'] / tracker['goal_days']
        tracker['progress_pct'] = max(0, min(100, int(round(progress_ratio * 100))))
    else:
        tracker['progress_pct'] = 0

    tracker_quote = tracker['quote'].strip()
    tracker['display_quote'] = tracker_quote if tracker_quote else 'Stay consistent today.'


def read_tracker_form(form_data):
    """
    Read and validate the tracker form.
    Returning a small dictionary keeps the route code easier to follow.
    """
    tracker_name = form_data.get('tracker_name', '').strip()
    tracker_type = form_data.get('tracker_type', '').strip().lower()
    quote_text = form_data.get('quote', '').strip()
    goal_days_text = form_data.get('goal_days', '').strip()

    has_any_input = any([tracker_name, quote_text, goal_days_text])
    if not has_any_input:
        return None, None

    if not tracker_name:
        return None, 'Tracker name is required.'

    if tracker_type not in TRACKER_TYPES:
        return None, 'Choose a valid tracker type.'

    goal_days = parse_goal_days(goal_days_text)
    if goal_days is None:
        return None, f'Goal days must be between {MIN_GOAL_DAYS} and {MAX_GOAL_DAYS}.'

    return {
        'name': tracker_name,
        'type': tracker_type,
        'quote': quote_text,
        'goal_days': goal_days,
    }, None


def create_tracker_for_user(user_id, tracker_data):
    db.create_tracker(
        user_id=user_id,
        tracker_name=tracker_data['name'],
        tracker_type=tracker_data['type'],
        quote_text=tracker_data['quote'],
        goal_days=tracker_data['goal_days'],
        start_date=today_text(),
    )


def get_owned_tracker(tracker_id):
    tracker = db.get_tracker(tracker_id)
    logged_in_user = get_logged_in_user()
    if tracker is None or logged_in_user is None:
        return None
    if tracker['user_id'] != logged_in_user['id']:
        return None
    return tracker


def render_trackers_setup_page(user):
    return render_template(
        'trackers_setup.html',
        user=user,
        trackers=db.get_trackers(user['id']),
        tracker_types=TRACKER_TYPES,
        max_trackers=MAX_TRACKERS,
        min_goal_days=MIN_GOAL_DAYS,
        max_goal_days=MAX_GOAL_DAYS,
        default_goal_days=DEFAULT_GOAL_DAYS,
    )


def redirect_to_dashboard(selected_index=None, month_offset=0):
    if selected_index is None:
        return redirect(url_for('dashboard'))
    return redirect(url_for('dashboard', p=selected_index, m=month_offset))


def today_text():
    return date.today().isoformat()


def clamp_selected_index(trackers, requested_index):
    if not trackers:
        return 0
    return max(0, min(requested_index, len(trackers) - 1))


def build_checkin_prompt(tracker, selected_index, month_offset, check_date_text):
    return {
        'tracker_id': tracker['id'],
        'tracker_name': tracker['name'],
        'date': check_date_text,
        'question_text': tracker['question_text'],
        'protocol_index': selected_index,
        'month_offset': month_offset,
    }


def get_active_checkin_cooldowns():
    """
    Keep only cooldowns that are still active so the session stays small
    and easy to understand.
    """
    saved_cooldowns = session.get('checkin_cooldowns', {})
    current_timestamp = int(time.time())
    active_cooldowns = {}

    for cooldown_key, expires_at in saved_cooldowns.items():
        expires_at = parse_int(expires_at)
        if expires_at and expires_at > current_timestamp:
            active_cooldowns[cooldown_key] = expires_at

    if active_cooldowns != saved_cooldowns:
        session['checkin_cooldowns'] = active_cooldowns

    return active_cooldowns


def build_checkin_cooldown_key(tracker_id, check_date_text):
    return f'{tracker_id}:{check_date_text}'


def is_checkin_prompt_on_cooldown(tracker_id, check_date_text):
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    return cooldown_key in get_active_checkin_cooldowns()


def save_checkin_prompt_cooldown(tracker_id, check_date_text):
    cooldowns = get_active_checkin_cooldowns()
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    cooldowns[cooldown_key] = int(time.time()) + CHECKIN_PROMPT_COOLDOWN_SECONDS
    session['checkin_cooldowns'] = cooldowns
    session.modified = True


def clear_checkin_prompt_cooldown(tracker_id, check_date_text):
    cooldowns = get_active_checkin_cooldowns()
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    if cooldown_key in cooldowns:
        cooldowns.pop(cooldown_key)
        session['checkin_cooldowns'] = cooldowns
        session.modified = True


def find_tracker_in_list(trackers, tracker_id):
    for tracker in trackers:
        if tracker['id'] == tracker_id:
            return tracker
    return None


def find_first_tracker_needing_prompt(trackers):
    current_day_text = today_text()
    for tracker_index, tracker in enumerate(trackers):
        if tracker['type'] not in ('habit', 'goal'):
            continue
        today_result = tracker['checkin_lookup'].get(current_day_text)
        if today_result in (True, False):
            continue
        if is_checkin_prompt_on_cooldown(tracker['id'], current_day_text):
            continue
        return tracker_index, tracker
    return None, None


def render_tracker_edit_page(user, tracker, selected_index, month_offset):
    return render_template(
        'tracker_edit.html',
        user=user,
        tracker=tracker,
        selected_index=selected_index,
        month_offset=month_offset,
        min_goal_days=MIN_GOAL_DAYS,
        max_goal_days=MAX_GOAL_DAYS,
    )


def render_signup_page():
    return render_template('signup.html')


def render_login_page():
    return render_template('login.html')


@app.route('/')
def index():
    if get_logged_in_user():
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if get_logged_in_user():
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()

        if not all([username, email, password]):
            flash('All fields are required.', 'error')
            return render_signup_page()

        if len(username) < MIN_USERNAME_LENGTH or len(username) > MAX_USERNAME_LENGTH:
            flash(f'Username must be between {MIN_USERNAME_LENGTH} and {MAX_USERNAME_LENGTH} characters.', 'error')
            return render_signup_page()

        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'error')
            return render_signup_page()

        if db.email_exists(email):
            flash('An account with that email already exists.', 'error')
            return render_signup_page()

        if db.username_exists(username):
            flash('That username is already taken.', 'error')
            return render_signup_page()

        user_id = db.create_user(username, email, hash_password(password))
        session['user_id'] = user_id
        return redirect(url_for('trackers_setup'))

    return render_signup_page()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_logged_in_user():
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()

        if not email or not password:
            flash('Please enter your email and password.', 'error')
            return render_login_page()

        user = db.get_user_by_email(email)
        if user is None or not password_matches(password, user['password_hash']):
            flash('Invalid credentials. Please try again.', 'error')
            return render_login_page()

        session['user_id'] = user['id']
        if db.get_tracker_count(user['id']) == 0:
            return redirect(url_for('trackers_setup'))
        return redirect(url_for('dashboard'))

    return render_login_page()


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/trackers/setup', methods=['GET', 'POST'])
def trackers_setup():
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    user_id = user['id']

    if request.method == 'POST':
        action_name = request.form.get('action', 'add')
        tracker_count = db.get_tracker_count(user_id)

        if tracker_count >= MAX_TRACKERS and action_name != 'finish':
            flash('You can add up to 3 trackers only.', 'error')
            return render_trackers_setup_page(user)

        submitted_tracker, form_error = read_tracker_form(request.form)
        if form_error:
            flash(form_error, 'error')
            return render_trackers_setup_page(user)

        if action_name == 'add':
            if submitted_tracker is None:
                flash('Enter tracker details before adding.', 'error')
            else:
                create_tracker_for_user(user_id, submitted_tracker)
                tracker_count += 1
                flash('Tracker added.', 'success')
            return render_trackers_setup_page(user)

        if submitted_tracker is not None:
            if tracker_count >= MAX_TRACKERS:
                flash('You can add up to 3 trackers only.', 'error')
                return render_trackers_setup_page(user)
            create_tracker_for_user(user_id, submitted_tracker)
            tracker_count += 1
            flash('Tracker added.', 'success')

        if tracker_count == 0:
            flash('Enter tracker details before finishing setup.', 'error')
            return render_trackers_setup_page(user)

        return redirect(url_for('dashboard'))

    return render_trackers_setup_page(user)


@app.route('/dashboard')
def dashboard():
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    trackers = db.get_trackers(user['id'])
    selected_index_text = request.args.get('p', '0').strip()
    month_offset_text = request.args.get('m', '0').strip()
    check_tracker_id_text = request.args.get('check_tracker', '').strip()
    check_date_text = request.args.get('check_date', '').strip()

    selected_index = parse_int(selected_index_text, 0)
    month_offset = parse_int(month_offset_text, 0)
    requested_tracker_id = parse_int(check_tracker_id_text)
    requested_check_date = parse_iso_date(check_date_text)
    selected_index = clamp_selected_index(trackers, selected_index)

    for tracker in trackers:
        prepare_tracker_for_dashboard(tracker, month_offset=month_offset)

    active_tracker = trackers[selected_index] if trackers else None
    requested_tracker = find_tracker_in_list(trackers, requested_tracker_id)
    checkin_prompt = None

    # A date clicked in the calendar gets first priority.
    # If there is no clicked date to ask about, we fall back to today's prompt.
    if (
        requested_tracker
        and requested_tracker['type'] in ('habit', 'goal')
        and requested_check_date
        and requested_check_date <= date.today()
        and not is_checkin_prompt_on_cooldown(requested_tracker['id'], requested_check_date.isoformat())
    ):
        checkin_prompt = build_checkin_prompt(
            tracker=requested_tracker,
            selected_index=selected_index,
            month_offset=month_offset,
            check_date_text=requested_check_date.isoformat(),
        )
    else:
        prompt_tracker_index, prompt_tracker = find_first_tracker_needing_prompt(trackers)
        if prompt_tracker is not None:
            checkin_prompt = build_checkin_prompt(
                tracker=prompt_tracker,
                selected_index=prompt_tracker_index,
                month_offset=month_offset,
                check_date_text=today_text(),
            )

    return render_template(
        'dashboard.html',
        user=user,
        trackers=trackers,
        active_tracker=active_tracker,
        selected_index=selected_index,
        month_offset=month_offset,
        total_trackers=len(trackers),
        max_trackers=MAX_TRACKERS,
        tracker_types=TRACKER_TYPES,
        checkin_prompt=checkin_prompt,
    )


@app.route('/trackers/<int:tracker_id>/edit', methods=['GET', 'POST'])
def edit_tracker(tracker_id):
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    tracker = get_owned_tracker(tracker_id)
    if tracker is None:
        flash('Tracker not found.', 'error')
        return redirect(url_for('dashboard'))

    selected_index = parse_int(request.values.get('p', '0').strip(), 0)
    month_offset = parse_int(request.values.get('m', '0').strip(), 0)
    prepare_tracker_for_dashboard(tracker)

    if request.method == 'GET':
        return render_tracker_edit_page(user, tracker, selected_index, month_offset)

    tracker_name = request.form.get('tracker_name', '').strip()
    quote_text = request.form.get('quote', '').strip()
    goal_days_text = request.form.get('goal_days', '').strip()

    if not tracker_name:
        flash('Tracker name cannot be empty.', 'error')
        return render_tracker_edit_page(user, tracker, selected_index, month_offset)

    goal_days = parse_goal_days(goal_days_text)
    if goal_days is None:
        flash(f'Goal days must be between {MIN_GOAL_DAYS} and {MAX_GOAL_DAYS}.', 'error')
        return render_tracker_edit_page(user, tracker, selected_index, month_offset)

    db.update_tracker(
        tracker_id=tracker_id,
        tracker_name=tracker_name,
        tracker_type=tracker['type'],
        quote_text=quote_text,
        goal_days=goal_days,
    )

    if goal_days < tracker['current_streak']:
        db.reset_tracker_progress(tracker_id, today_text())
        flash('Tracker updated. Progress reset from today to match the new goal.', 'success')
    else:
        flash('Tracker updated.', 'success')

    return redirect_to_dashboard(selected_index, month_offset)


@app.route('/trackers/<int:tracker_id>/checkin', methods=['POST'])
def checkin_tracker(tracker_id):
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    tracker = get_owned_tracker(tracker_id)
    if tracker is None:
        flash('Tracker not found.', 'error')
        return redirect(url_for('dashboard'))

    if tracker['type'] not in ('habit', 'goal'):
        flash('Check-in is only available for habit and goal trackers.', 'error')
        return redirect(url_for('dashboard'))

    selected_index = parse_int(request.form.get('protocol_index', '').strip())
    month_offset = parse_int(request.form.get('month_offset', '').strip(), 0)
    checkin_answer = request.form.get('status')

    if checkin_answer not in ('yes', 'no', 'not_now'):
        flash('Invalid check-in status.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    check_date = parse_iso_date(request.form.get('check_date', '').strip()) or date.today()
    today = date.today()
    if check_date > today:
        flash('Cannot check in for a future date.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    tracker_start_date = parse_iso_date(tracker['start_date']) or today
    if check_date < tracker_start_date:
        flash('Cannot check in before the tracker start date.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    check_date_text = check_date.isoformat()

    if checkin_answer == 'not_now':
        save_checkin_prompt_cooldown(tracker_id, check_date_text)
        flash(f"{tracker['name']}: popup snoozed for 5 minutes.", 'success')
        return redirect_to_dashboard(selected_index, month_offset)

    clear_checkin_prompt_cooldown(tracker_id, check_date_text)
    saved_status = 'clean' if checkin_answer == 'yes' else 'slipped'
    db.save_checkin(
        tracker_id=tracker_id,
        check_date=check_date_text,
        status=saved_status,
        timestamp=int(time.time()),
    )

    if saved_status == 'slipped':
        flash(f"{tracker['name']}: streak reset to 0.", 'error')
    else:
        flash(f"{tracker['name']}: streak +1.", 'success')

    return redirect_to_dashboard(selected_index, month_offset)


if __name__ == '__main__':
    app.run(debug=True)
