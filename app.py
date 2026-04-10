import hashlib
import time
from datetime import date, datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for

import database as db

# Create the main Flask application object.
app = Flask(__name__)
# Flask uses this key to safely sign session data, like the logged-in user id.
app.secret_key = 'dev-secret-key'

# ----------------------------
# Project-wide configuration
# ----------------------------
# We store dates as text in the database using this format.
DATE_FORMAT = '%Y-%m-%d'
# Each user can create at most three trackers.
MAX_TRACKERS = 3
# Reuse the allowed tracker types from the database module so both files stay consistent.
TRACKER_TYPES = db.TRACKER_TYPES
# Only habits and goals support daily yes/no check-ins.
CHECKIN_TRACKER_TYPES = ('habit', 'goal')
# Simple signup rules for this beginner project.
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 20
MIN_PASSWORD_LENGTH = 6
# Reuse the day limits from the database layer.
MIN_GOAL_DAYS = db.MIN_GOAL_DAYS
MAX_GOAL_DAYS = db.MAX_GOAL_DAYS
DEFAULT_GOAL_DAYS = db.DEFAULT_GOAL_DAYS
# The dashboard calendar always shows six full weeks.
CALENDAR_GRID_DAY_COUNT = 42
# The popup sleeps for 5 minutes after "NOT NOW".
CHECKIN_PROMPT_COOLDOWN_SECONDS = 5 * 60
# Use this quote when the user has not written one yet.
DEFAULT_TRACKER_QUOTE = 'Stay consistent today.'
# These are the only valid answers from the popup buttons.
CHECKIN_ANSWERS = ('yes', 'no', 'not_now')
# Convert the popup answer into the value we save in the database.
CHECKIN_STATUS_FOR_ANSWER = {
    'yes': 'clean',
    'no': 'slipped',
}
# Convert saved database status values into simple Python values.
# True  -> success day
# False -> slipped day
# None  -> unsure / no final answer
CHECKIN_RESULT_BY_STATUS = {
    'clean': True,
    'slipped': False,
    'unsure': None,
}
# Change this value if you want the popup to wait longer or shorter before showing again.

# Build the database tables when the app starts.
db.init_db()


# ----------------------------
# Simple auth helpers
# ----------------------------
def hash_password(password):
    """Turn a plain password into a one-way hash before saving it."""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def password_matches(password, saved_password_hash):
    """Check whether the entered password matches the saved password hash."""
    return hash_password(password) == saved_password_hash


def get_logged_in_user():
    """Read the logged-in user id from the session and fetch that user."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.get_user_by_id(user_id)


# ----------------------------
# Parsing and validation helpers
# ----------------------------
def parse_int(text_value, fallback=None):
    """Safely turn text into an integer without crashing on bad input."""
    try:
        return int(text_value)
    except (TypeError, ValueError):
        return fallback


def parse_goal_days(goal_days_text):
    """Read and validate the goal day value from a form field."""
    goal_days = parse_int(goal_days_text)
    if goal_days is None:
        return None
    if MIN_GOAL_DAYS <= goal_days <= MAX_GOAL_DAYS:
        return goal_days
    return None


def parse_iso_date(date_text):
    """Read a YYYY-MM-DD date string and turn it into a Python date object."""
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, DATE_FORMAT).date()
    except ValueError:
        return None


def today_text():
    """Return today's date as the same text format used in the database."""
    return date.today().isoformat()


def tracker_supports_checkin(tracker_type):
    """Only habits and goals use the daily yes/no popup flow."""
    return tracker_type in CHECKIN_TRACKER_TYPES


def read_tracker_form(form_data, forced_tracker_type=None, allow_empty=False):
    """
    Read one tracker form in a single place so setup and edit use the same
    validation rules.
    """
    # Read the raw values exactly as the browser sends them.
    tracker_name = form_data.get('tracker_name', '').strip()
    # During edit, the type stays fixed, so we can force it from the tracker row.
    tracker_type = forced_tracker_type or form_data.get('tracker_type', '').strip().lower()
    quote_text = form_data.get('quote', '').strip()
    goal_days_text = form_data.get('goal_days', '').strip()

    # The setup page allows an empty form when the user clicks finish without
    # trying to add a new tracker in that same request.
    if allow_empty and not any([tracker_name, quote_text, goal_days_text]):
        return None, None

    # Name is required for both add and edit flows.
    if not tracker_name:
        return None, 'Tracker name is required.'

    # The type must be one of the allowed project tracker types.
    if tracker_type not in TRACKER_TYPES:
        return None, 'Choose a valid tracker type.'

    # Goal days must be a number inside the allowed range.
    goal_days = parse_goal_days(goal_days_text)
    if goal_days is None:
        return None, f'Goal days must be between {MIN_GOAL_DAYS} and {MAX_GOAL_DAYS}.'

    # Return a small clean dictionary that the routes can use directly.
    return {
        'name': tracker_name,
        'type': tracker_type,
        'quote': quote_text,
        'goal_days': goal_days,
    }, None


# ----------------------------
# Dashboard data builders
# ----------------------------
def build_checkin_lookup(checkin_rows):
    """Turn a list of database rows into a date -> result lookup table."""
    checkin_lookup = {}
    for checkin_row in checkin_rows:
        checkin_lookup[checkin_row['date']] = CHECKIN_RESULT_BY_STATUS.get(checkin_row['status'])
    return checkin_lookup


def update_tracker_streak(tracker):
    """
    Count the current streak by walking through saved days in date order.
    A clean day adds 1. A slipped day resets the streak to 0.
    """
    current_streak = 0

    for day_text in sorted(tracker['checkin_lookup']):
        day_result = tracker['checkin_lookup'][day_text]
        if day_result is True:
            current_streak += 1
            continue
        if day_result is False:
            current_streak = 0

    tracker['current_streak'] = current_streak
    tracker['streak_unit'] = 'day' if current_streak in (0, 1) else 'days'


def shift_month_start(base_month_start, month_offset):
    """Move forward or backward by whole months from a starting month."""
    month_number = (base_month_start.year * 12 + (base_month_start.month - 1)) + month_offset
    target_year = month_number // 12
    target_month = (month_number % 12) + 1
    return date(target_year, target_month, 1)


def build_calendar_cell(
    current_day,
    month_start,
    today,
    tracker_start_date,
    can_open_checkin,
    checkin_lookup,
):
    """
    Build one calendar cell for the dashboard.
    This decides the day label, its color, and whether the user can click it.
    """
    day_text = current_day.isoformat()
    is_inside_month = (
        current_day.year == month_start.year
        and current_day.month == month_start.month
    )

    # Days outside the current month are hidden placeholders.
    if not is_inside_month:
        cell_style = 'outside'
        is_clickable = False
    # Future days and days before the tracker start date cannot be checked in.
    elif current_day > today or current_day < tracker_start_date:
        cell_style = 'pending'
        is_clickable = False
    else:
        # For valid days inside the month, show the saved result if one exists.
        checkin_result = checkin_lookup.get(day_text)
        if checkin_result is True:
            cell_style = 'done'
        elif checkin_result is False:
            cell_style = 'miss'
        else:
            cell_style = 'pending'
        is_clickable = can_open_checkin

    return {
        'date': day_text,
        'day': current_day.strftime('%d'),
        'style': cell_style,
        'clickable': is_clickable,
        'in_month': is_inside_month,
    }


def build_calendar_cells(tracker, month_offset=0):
    """Build the full six-week calendar grid shown on the dashboard."""
    today = date.today()
    tracker_start_date = parse_iso_date(tracker.get('start_date')) or today
    month_start = shift_month_start(today.replace(day=1), month_offset)
    # Start from the Monday of the first visible week.
    calendar_start = month_start - timedelta(days=month_start.weekday())
    can_open_checkin = tracker_supports_checkin(tracker['type'])
    calendar_cells = []

    for day_offset in range(CALENDAR_GRID_DAY_COUNT):
        current_day = calendar_start + timedelta(days=day_offset)
        calendar_cells.append(
            build_calendar_cell(
                current_day=current_day,
                month_start=month_start,
                today=today,
                tracker_start_date=tracker_start_date,
                can_open_checkin=can_open_checkin,
                checkin_lookup=tracker['checkin_lookup'],
            )
        )

    tracker['calendar'] = calendar_cells
    tracker['month_label'] = month_start.strftime('%B %Y')


def prepare_tracker_for_dashboard(tracker, month_offset=0):
    """
    The database stores only the raw tracker row and its check-in logs.
    This helper builds the extra values the dashboard needs to display.
    """
    # Load all saved yes/no results for this tracker.
    tracker['checkin_lookup'] = build_checkin_lookup(db.get_checkin_logs(tracker['id']))
    # Count the current streak from those saved results.
    update_tracker_streak(tracker)
    # Build the visible month grid for the dashboard.
    build_calendar_cells(tracker, month_offset=month_offset)

    # Habits use "free for", while goals and subjects use "focusing on".
    if tracker['type'] == 'habit':
        tracker['question_text'] = 'Did you avoid the habit?'
        tracker['headline_text'] = f"You been {tracker['name']} free for :"
    else:
        tracker['question_text'] = 'Did you make progress on this today?'
        tracker['headline_text'] = f"You been focusing on {tracker['name']} for:"

    # Convert current streak into a 0-100 progress bar value.
    goal_days = tracker.get('goal_days') or DEFAULT_GOAL_DAYS
    progress_ratio = tracker['current_streak'] / goal_days if goal_days else 0
    tracker['progress_pct'] = max(0, min(100, int(round(progress_ratio * 100))))

    # Show the saved quote, or fall back to a default quote.
    tracker_quote = tracker.get('quote', '').strip()
    tracker['display_quote'] = tracker_quote if tracker_quote else DEFAULT_TRACKER_QUOTE


def load_dashboard_trackers(user_id, month_offset):
    """Load all trackers for one user and prepare each one for the dashboard."""
    trackers = db.get_trackers(user_id)
    for tracker in trackers:
        prepare_tracker_for_dashboard(tracker, month_offset=month_offset)
    return trackers


# ----------------------------
# Small view helpers
# ----------------------------
def create_tracker_for_user(user_id, tracker_data):
    """Save one new tracker for the logged-in user."""
    db.create_tracker(
        user_id=user_id,
        tracker_name=tracker_data['name'],
        tracker_type=tracker_data['type'],
        quote_text=tracker_data['quote'],
        goal_days=tracker_data['goal_days'],
        start_date=today_text(),
    )


def get_owned_tracker(tracker_id, user_id):
    """Return a tracker only if it belongs to the logged-in user."""
    tracker = db.get_tracker(tracker_id)
    if tracker is None:
        return None
    if tracker['user_id'] != user_id:
        return None
    return tracker


def render_trackers_setup_page(user, trackers=None):
    """Render the setup page with the latest tracker list."""
    if trackers is None:
        trackers = db.get_trackers(user['id'])

    return render_template(
        'trackers_setup.html',
        user=user,
        trackers=trackers,
        tracker_types=TRACKER_TYPES,
        max_trackers=MAX_TRACKERS,
        min_goal_days=MIN_GOAL_DAYS,
        max_goal_days=MAX_GOAL_DAYS,
        default_goal_days=DEFAULT_GOAL_DAYS,
    )


def redirect_to_dashboard(selected_index=None, month_offset=0):
    """Send the user back to the same dashboard view after an action."""
    if selected_index is None:
        return redirect(url_for('dashboard'))
    return redirect(url_for('dashboard', p=selected_index, m=month_offset))


def clamp_selected_index(trackers, requested_index):
    """Keep the selected tracker index inside the valid range."""
    if not trackers:
        return 0
    return max(0, min(requested_index, len(trackers) - 1))


def build_checkin_prompt(tracker, selected_index, month_offset, check_date_text):
    """Build the small dictionary the modal popup needs."""
    return {
        'tracker_id': tracker['id'],
        'tracker_name': tracker['name'],
        'date': check_date_text,
        'question_text': tracker['question_text'],
        'protocol_index': selected_index,
        'month_offset': month_offset,
    }


# ----------------------------
# Popup cooldown helpers
# ----------------------------
def get_active_checkin_cooldowns():
    """
    Keep only cooldowns that are still active so the session stays small
    and easy to understand.
    """
    # Read the saved cooldown map from the user's session.
    saved_cooldowns = session.get('checkin_cooldowns', {})
    current_timestamp = int(time.time())
    active_cooldowns = {}

    for cooldown_key, expires_at in saved_cooldowns.items():
        # Ignore invalid values and keep only future expiry times.
        expires_at = parse_int(expires_at)
        if expires_at and expires_at > current_timestamp:
            active_cooldowns[cooldown_key] = expires_at

    # Save the cleaned version back into the session if anything expired.
    if active_cooldowns != saved_cooldowns:
        session['checkin_cooldowns'] = active_cooldowns

    return active_cooldowns


def build_checkin_cooldown_key(tracker_id, check_date_text):
    """Use one text key per tracker/day pair inside the session."""
    return f'{tracker_id}:{check_date_text}'


def is_checkin_prompt_on_cooldown(tracker_id, check_date_text):
    """Check whether the popup is currently snoozed for this tracker/day."""
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    return cooldown_key in get_active_checkin_cooldowns()


def save_checkin_prompt_cooldown(tracker_id, check_date_text):
    """Save a new 5-minute cooldown after the user clicks NOT NOW."""
    cooldowns = get_active_checkin_cooldowns()
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    cooldowns[cooldown_key] = int(time.time()) + CHECKIN_PROMPT_COOLDOWN_SECONDS
    session['checkin_cooldowns'] = cooldowns
    session.modified = True


def clear_checkin_prompt_cooldown(tracker_id, check_date_text):
    """Remove the cooldown after the user gives a real yes/no answer."""
    cooldowns = get_active_checkin_cooldowns()
    cooldown_key = build_checkin_cooldown_key(tracker_id, check_date_text)
    if cooldown_key not in cooldowns:
        return
    cooldowns.pop(cooldown_key)
    session['checkin_cooldowns'] = cooldowns
    session.modified = True


# ----------------------------
# Popup selection helpers
# ----------------------------
def find_tracker_in_list(trackers, tracker_id):
    """Find one tracker inside an already-loaded tracker list."""
    for tracker in trackers:
        if tracker['id'] == tracker_id:
            return tracker
    return None


def find_first_tracker_needing_prompt(trackers):
    """Find the first habit/goal tracker that still needs today's answer."""
    current_day_text = today_text()
    for tracker_index, tracker in enumerate(trackers):
        if not tracker_supports_checkin(tracker['type']):
            continue
        today_result = tracker['checkin_lookup'].get(current_day_text)
        if today_result in (True, False):
            continue
        if is_checkin_prompt_on_cooldown(tracker['id'], current_day_text):
            continue
        return tracker_index, tracker
    return None, None


def find_requested_checkin_prompt(
    trackers,
    selected_index,
    month_offset,
    requested_tracker_id,
    requested_check_date,
):
    """Build a popup request when the user clicks a day in the calendar."""
    requested_tracker = find_tracker_in_list(trackers, requested_tracker_id)
    if requested_tracker is None:
        return None
    if not tracker_supports_checkin(requested_tracker['type']):
        return None
    if requested_check_date is None:
        return None
    if requested_check_date > date.today():
        return None

    tracker_start_date = parse_iso_date(requested_tracker.get('start_date')) or date.today()
    if requested_check_date < tracker_start_date:
        return None

    check_date_text = requested_check_date.isoformat()
    if is_checkin_prompt_on_cooldown(requested_tracker['id'], check_date_text):
        return None

    return build_checkin_prompt(
        tracker=requested_tracker,
        selected_index=selected_index,
        month_offset=month_offset,
        check_date_text=check_date_text,
    )


def choose_checkin_prompt(
    trackers,
    selected_index,
    month_offset,
    requested_tracker_id,
    requested_check_date,
):
    """
    Choose which popup to show.
    1. A clicked calendar date gets first priority.
    2. If nothing was clicked, fall back to today's first missing tracker.
    """
    requested_prompt = find_requested_checkin_prompt(
        trackers=trackers,
        selected_index=selected_index,
        month_offset=month_offset,
        requested_tracker_id=requested_tracker_id,
        requested_check_date=requested_check_date,
    )
    if requested_prompt is not None:
        return requested_prompt

    prompt_tracker_index, prompt_tracker = find_first_tracker_needing_prompt(trackers)
    if prompt_tracker is None:
        return None

    return build_checkin_prompt(
        tracker=prompt_tracker,
        selected_index=prompt_tracker_index,
        month_offset=month_offset,
        check_date_text=today_text(),
    )


def render_tracker_edit_page(user, tracker, selected_index, month_offset):
    """Render the edit page for the currently selected tracker."""
    return render_template(
        'tracker_edit.html',
        user=user,
        tracker=tracker,
        selected_index=selected_index,
        month_offset=month_offset,
        min_goal_days=MIN_GOAL_DAYS,
        max_goal_days=MAX_GOAL_DAYS,
    )


# ----------------------------
# Routes
# ----------------------------
@app.route('/')
def index():
    """Home page."""
    if get_logged_in_user():
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Create a new account and log the user in immediately."""
    if get_logged_in_user():
        return redirect(url_for('dashboard'))

    # A normal page visit just shows the form.
    if request.method != 'POST':
        return render_template('signup.html')

    # Read the form fields.
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '').strip()

    # Stop early if any required field is missing.
    if not all([username, email, password]):
        flash('All fields are required.', 'error')
        return render_template('signup.html')

    # Keep the username inside the allowed length range.
    if len(username) < MIN_USERNAME_LENGTH or len(username) > MAX_USERNAME_LENGTH:
        flash(
            f'Username must be between {MIN_USERNAME_LENGTH} and {MAX_USERNAME_LENGTH} characters.',
            'error',
        )
        return render_template('signup.html')

    # Require a minimum password length.
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'error')
        return render_template('signup.html')

    # Email must stay unique across all users.
    if db.email_exists(email):
        flash('An account with that email already exists.', 'error')
        return render_template('signup.html')

    # Username must also stay unique.
    if db.username_exists(username):
        flash('That username is already taken.', 'error')
        return render_template('signup.html')

    # Save the new user and remember them in the Flask session.
    user_id = db.create_user(username, email, hash_password(password))
    session['user_id'] = user_id
    # New users go straight to tracker setup.
    return redirect(url_for('trackers_setup'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Log an existing user in."""
    if get_logged_in_user():
        return redirect(url_for('dashboard'))

    # A normal page visit just shows the form.
    if request.method != 'POST':
        return render_template('login.html')

    # Read the login form fields.
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '').strip()

    # Both fields are required.
    if not email or not password:
        flash('Please enter your email and password.', 'error')
        return render_template('login.html')

    # Load the user and compare the entered password with the saved hash.
    user = db.get_user_by_email(email)
    if user is None or not password_matches(password, user['password_hash']):
        flash('Invalid credentials. Please try again.', 'error')
        return render_template('login.html')

    # Save the user id in the session after a successful login.
    session['user_id'] = user['id']
    # Brand-new users with no trackers go to setup first.
    if db.get_tracker_count(user['id']) == 0:
        return redirect(url_for('trackers_setup'))
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    """Log out by clearing the whole session."""
    session.clear()
    return redirect(url_for('index'))


@app.route('/trackers/setup', methods=['GET', 'POST'])
def trackers_setup():
    """Create trackers after signup or when adding more later."""
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    user_id = user['id']
    # Load the current tracker list once for this request.
    trackers = db.get_trackers(user_id)

    # A normal page visit just shows the page.
    if request.method != 'POST':
        return render_trackers_setup_page(user, trackers)

    # The form can either add one tracker or finish setup.
    action_name = request.form.get('action', 'add')
    tracker_count = len(trackers)

    # Block adding if the user already has the maximum number of trackers.
    if tracker_count >= MAX_TRACKERS and action_name != 'finish':
        flash(f'You can add up to {MAX_TRACKERS} trackers only.', 'error')
        return render_trackers_setup_page(user, trackers)

    # Read the tracker fields using the shared tracker form helper.
    submitted_tracker, form_error = read_tracker_form(request.form, allow_empty=True)
    if form_error:
        flash(form_error, 'error')
        return render_trackers_setup_page(user, trackers)

    if action_name == 'add':
        # The user clicked ADD TRACKER but left the form empty.
        if submitted_tracker is None:
            flash('Enter tracker details before adding.', 'error')
            return render_trackers_setup_page(user, trackers)

        # Save the new tracker and refresh the page.
        create_tracker_for_user(user_id, submitted_tracker)
        flash('Tracker added.', 'success')
        return render_trackers_setup_page(user)

    # If the user clicked FINISH and also typed tracker details, save them too.
    if submitted_tracker is not None:
        if tracker_count >= MAX_TRACKERS:
            flash(f'You can add up to {MAX_TRACKERS} trackers only.', 'error')
            return render_trackers_setup_page(user, trackers)

        create_tracker_for_user(user_id, submitted_tracker)
        tracker_count += 1
        flash('Tracker added.', 'success')

    # Finishing with zero trackers is not allowed.
    if tracker_count == 0:
        flash('Enter tracker details before finishing setup.', 'error')
        return render_trackers_setup_page(user, trackers)

    # Setup is complete, so go to the dashboard.
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    """Main dashboard page."""
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    # Read dashboard state from the URL query string.
    selected_index_text = request.args.get('p', '0').strip()
    month_offset_text = request.args.get('m', '0').strip()
    check_tracker_id_text = request.args.get('check_tracker', '').strip()
    check_date_text = request.args.get('check_date', '').strip()

    # Convert raw query text into safe Python values.
    selected_index = parse_int(selected_index_text, 0)
    month_offset = parse_int(month_offset_text, 0)
    requested_tracker_id = parse_int(check_tracker_id_text)
    requested_check_date = parse_iso_date(check_date_text)

    # Load and prepare every tracker for dashboard display.
    trackers = load_dashboard_trackers(user['id'], month_offset)
    # Keep the selected index inside the valid range.
    selected_index = clamp_selected_index(trackers, selected_index)
    active_tracker = trackers[selected_index] if trackers else None

    # Decide whether the yes/no popup should be shown right now.
    checkin_prompt = choose_checkin_prompt(
        trackers=trackers,
        selected_index=selected_index,
        month_offset=month_offset,
        requested_tracker_id=requested_tracker_id,
        requested_check_date=requested_check_date,
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
    """Edit one tracker that belongs to the logged-in user."""
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    # Never allow a user to edit someone else's tracker.
    tracker = get_owned_tracker(tracker_id, user['id'])
    if tracker is None:
        flash('Tracker not found.', 'error')
        return redirect(url_for('dashboard'))

    # Keep the dashboard position so we can return to the same place later.
    selected_index = parse_int(request.values.get('p', '0').strip(), 0)
    month_offset = parse_int(request.values.get('m', '0').strip(), 0)
    # Build streak/progress values for the reset comparison message.
    prepare_tracker_for_dashboard(tracker)

    if request.method == 'GET':
        return render_tracker_edit_page(user, tracker, selected_index, month_offset)

    # Reuse the same form parser as the setup page, but keep the old type fixed.
    updated_tracker, form_error = read_tracker_form(
        request.form,
        forced_tracker_type=tracker['type'],
    )
    if form_error:
        flash(form_error, 'error')
        return render_tracker_edit_page(user, tracker, selected_index, month_offset)

    # Save the new tracker values.
    db.update_tracker(
        tracker_id=tracker_id,
        tracker_name=updated_tracker['name'],
        tracker_type=updated_tracker['type'],
        quote_text=updated_tracker['quote'],
        goal_days=updated_tracker['goal_days'],
    )

    # If the new goal is smaller than the current streak, restart the tracker
    # so the old progress does not look incorrect.
    if updated_tracker['goal_days'] < tracker['current_streak']:
        db.reset_tracker_progress(tracker_id, today_text())
        flash('Tracker updated. Progress reset from today to match the new goal.', 'success')
    else:
        flash('Tracker updated.', 'success')

    return redirect_to_dashboard(selected_index, month_offset)


@app.route('/trackers/<int:tracker_id>/checkin', methods=['POST'])
def checkin_tracker(tracker_id):
    """Handle one yes/no/not now response from the popup."""
    user = get_logged_in_user()
    if user is None:
        return redirect(url_for('login'))

    # Only allow check-ins on the owner's own tracker.
    tracker = get_owned_tracker(tracker_id, user['id'])
    if tracker is None:
        flash('Tracker not found.', 'error')
        return redirect(url_for('dashboard'))

    # Subjects do not use daily yes/no check-ins.
    if not tracker_supports_checkin(tracker['type']):
        flash('Check-in is only available for habit and goal trackers.', 'error')
        return redirect(url_for('dashboard'))

    # Read the hidden dashboard state so we can return to the same view.
    selected_index = parse_int(request.form.get('protocol_index', '').strip())
    month_offset = parse_int(request.form.get('month_offset', '').strip(), 0)
    checkin_answer = request.form.get('status')

    # Accept only the three known button values.
    if checkin_answer not in CHECKIN_ANSWERS:
        flash('Invalid check-in status.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    # Read the date the user is answering for.
    check_date = parse_iso_date(request.form.get('check_date', '').strip()) or date.today()
    today = date.today()
    # Future dates are never valid.
    if check_date > today:
        flash('Cannot check in for a future date.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    # Dates before the tracker started are also invalid.
    tracker_start_date = parse_iso_date(tracker['start_date']) or today
    if check_date < tracker_start_date:
        flash('Cannot check in before the tracker start date.', 'error')
        return redirect_to_dashboard(selected_index, month_offset)

    check_date_text = check_date.isoformat()

    # NOT NOW does not save a streak result. It only snoozes the popup.
    if checkin_answer == 'not_now':
        save_checkin_prompt_cooldown(tracker_id, check_date_text)
        flash(f"{tracker['name']}: popup snoozed for 5 minutes.", 'success')
        return redirect_to_dashboard(selected_index, month_offset)

    # A real yes/no answer clears any cooldown for that same day.
    clear_checkin_prompt_cooldown(tracker_id, check_date_text)
    # Convert the button answer into the saved database status.
    saved_status = CHECKIN_STATUS_FOR_ANSWER[checkin_answer]
    # Save or update the check-in row for that day.
    db.save_checkin(
        tracker_id=tracker_id,
        check_date=check_date_text,
        status=saved_status,
        timestamp=int(time.time()),
    )

    # Show a simple success/error flash message after saving.
    if saved_status == 'slipped':
        flash(f"{tracker['name']}: streak reset to 0.", 'error')
    else:
        flash(f"{tracker['name']}: streak +1.", 'success')

    return redirect_to_dashboard(selected_index, month_offset)


if __name__ == '__main__':
    # Run the local development server when this file is executed directly.
    app.run(debug=True)
