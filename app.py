import os
import hmac
import json
import time
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta
from sqlalchemy import or_, and_
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, abort, session)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from models import (db, Game, Team, Fund, CompanyTemplate, GameCompany,
                    CompanySearch, TermSheet, Deal, DealEquity,
                    FundTransaction, Notification, ReturnAssumption,
                    ebitda_to_cash)
from game_logic import (run_phase1_crank, run_phase2_crank,
                        team_simple_return, team_gp_income,
                        finalize_deal, close_deal_with_coinvestors,
                        locked_deal_economics, exit_waterfall, process_followon,
                        DEBT_INTEREST_RATE, _notify, _record_transaction)
import game_settings

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')

# Use absolute path for SQLite so the same DB is used regardless of working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_BASE_DIR, 'simulategp.db').replace('\\', '/')
_DEFAULT_DB = f'sqlite:///{_DB_PATH}'
_db_url = os.environ.get('DATABASE_URL', _DEFAULT_DB)
# Railway/Heroku provide postgres:// but SQLAlchemy 2.x requires postgresql+psycopg2://
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql+psycopg2://', 1)
elif _db_url.startswith('postgresql://') and '+' not in _db_url.split('://')[0]:
    _db_url = _db_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the simulation.'


@login_manager.user_loader
def load_user(user_id):
    return Team.query.get(int(user_id))


# ---------------------------------------------------------------------------
# Context Processors
# ---------------------------------------------------------------------------

@app.context_processor
def inject_game():
    game = None
    if current_user.is_authenticated:
        game = Game.query.get(current_user.game_id)
    return dict(game=game)


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Rate limiting for the public endpoints (login / create-game / join-game) so
# bots can't brute-force passwords, access codes, or join codes. In-memory
# sliding window per client IP — fine for the single-worker gunicorn setup.
# ---------------------------------------------------------------------------

_RATE_BUCKETS = defaultdict(deque)


def _rate_limited(bucket, limit, window_secs):
    """True if this request pushes the (bucket, client-ip) count over `limit`
    within the trailing window. Uses X-Forwarded-For because production sits
    behind Railway's proxy (remote_addr would be the proxy for everyone)."""
    ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '?')
          .split(',')[0].strip())
    now = time.time()
    q = _RATE_BUCKETS[(bucket, ip)]
    while q and now - q[0] > window_secs:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        if _rate_limited('login', 20, 300):
            flash('Too many login attempts — please wait a few minutes.',
                  'warning')
            return redirect(url_for('login'))
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        team = Team.query.filter_by(username=username).first()
        if team and team.check_password(password):
            if not team.is_admin:
                team_game = Game.query.get(team.game_id)
                if team_game and team_game.is_archived:
                    flash('This game has been archived by the instructor — '
                          'its logins are disabled.', 'warning')
                    return redirect(url_for('login'))
            login_user(team, remember=True)
            team.last_login = datetime.utcnow()
            team.last_seen = team.last_login
            db.session.commit()
            return redirect(url_for('index'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/guide')
def guide():
    """First-time player guide. Public, so students can read it before they
    create or join a game."""
    return render_template('guide.html')


# Cap on active (non-archived) games so the self-service page can't flood the
# DB — each game seeds the full company catalog.
MAX_ACTIVE_GAMES = 40

# Join codes avoid easily-confused characters (no I/L/O/0/1).
_JOIN_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _new_join_code():
    """Generate a unique 6-character join code for a game."""
    while True:
        code = ''.join(random.choices(_JOIN_CODE_ALPHABET, k=6))
        if not Game.query.filter_by(join_code=code).first():
            return code


# The only LP fee structures on offer: management fee & carried interest.
FEE_STRUCTURES = {
    '2_20': (0.02, 0.20),   # standard
    '1_10': (0.01, 0.10),   # low-fee
}


def _parse_fee_structure(field='fee_structure'):
    """Resolve the chosen fee structure to (mgmt_fee, perf_fee) decimals.
    Anything unrecognized falls back to the standard 2 & 20."""
    return FEE_STRUCTURES.get(request.form.get(field), FEE_STRUCTURES['2_20'])


def _parse_team_signup_form():
    """Sanitize the shared team-signup fields (create-game and join-game)."""
    firm_name = (request.form.get('firm_name') or '').strip()
    username = (request.form.get('username') or '').strip()
    password = (request.form.get('password') or '').strip()
    fund_type = request.form.get('fund_type', 'pe')
    if fund_type not in ('pe', 'vc'):
        fund_type = 'pe'
    sector_focus = request.form.get('sector_focus', 'generalist')
    if sector_focus != 'generalist' and sector_focus not in SECTORS:
        sector_focus = 'generalist'
    try:
        fund_size = float(request.form.get('fund_size', 500))
    except ValueError:
        fund_size = 500.0
    if int(fund_size) not in FUND_SIZE_PARTNERS:
        fund_size = 500.0
    mgmt_fee, perf_fee = _parse_fee_structure()
    return (firm_name, username, password, fund_type, sector_focus, fund_size,
            mgmt_fee, perf_fee)


def _create_team_with_fund(game, firm_name, username, password,
                           fund_type, sector_focus, fund_size,
                           mgmt_fee=0.02, perf_fee=0.20):
    """Create a team in `game` with its Fund I at the team's chosen LP terms
    (management fee and carried interest as decimals). Caller commits."""
    team = Team(
        game_id=game.id,
        username=username,
        firm_name=firm_name,
        reputation=5.0,
        sector_focus=sector_focus,
        fund_type=fund_type,
        num_partners=FUND_SIZE_PARTNERS.get(int(fund_size), 5),
    )
    team.set_password(password)
    db.session.add(team)
    db.session.flush()
    db.session.add(Fund(
        team_id=team.id,
        name=f'{firm_name} Fund I',
        total_capital=fund_size,
        available_capital=fund_size,
        year_raised=game.current_year,
        management_fee_rate=mgmt_fee,
        performance_fee_rate=perf_fee,
        operating_cost_rate=FUND_SIZE_OPEX.get(int(fund_size), 0.01),
    ))
    return team


@app.route('/create-game', methods=['GET', 'POST'])
def create_game_self_service():
    """Self-service sandbox: with the instructor's access code, a visitor
    creates a fresh game plus their own team login in one step and is signed
    straight in. The code is set via the GAME_CREATE_CODE environment variable
    (change it there to rotate or disable access)."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        if _rate_limited('create_game', 10, 600):
            flash('Too many attempts — please wait a few minutes.', 'warning')
            return redirect(url_for('create_game_self_service'))
        code = (request.form.get('access_code') or '').strip()
        # No default in production: unless GAME_CREATE_CODE is set, creation is
        # disabled (a hardcoded fallback would be readable in the public repo).
        # Local dev (debug) keeps 'letmetry' for convenience.
        expected = os.environ.get('GAME_CREATE_CODE') or \
            ('letmetry' if app.debug else None)
        if not expected:
            flash('Self-service game creation is currently disabled.', 'warning')
            return redirect(url_for('create_game_self_service'))
        if not code or not hmac.compare_digest(code, expected):
            flash('Invalid access code.', 'danger')
            return redirect(url_for('create_game_self_service'))

        (firm_name, username, password, fund_type, sector_focus, fund_size,
         mgmt_fee, perf_fee) = _parse_team_signup_form()
        game_name = (request.form.get('game_name') or '').strip()

        if not firm_name or not username or not password:
            flash('Firm name, username, and password are all required.', 'danger')
            return redirect(url_for('create_game_self_service'))
        if Team.query.filter_by(username=username).first():
            flash(f'Username "{username}" is already taken — pick another.',
                  'danger')
            return redirect(url_for('create_game_self_service'))
        if (Game.query.filter_by(is_archived=False).count()
                >= MAX_ACTIVE_GAMES):
            flash('Too many active games right now — please contact your '
                  'instructor.', 'warning')
            return redirect(url_for('create_game_self_service'))

        game = Game(name=game_name or f'{firm_name} — Test Game',
                    current_year=1, current_phase=1, status='active',
                    join_code=_new_join_code())
        db.session.add(game)
        db.session.flush()
        _seed_companies(game)

        team = _create_team_with_fund(game, firm_name, username, password,
                                      fund_type, sector_focus, fund_size,
                                      mgmt_fee, perf_fee)
        game.owner_id = team.id   # creator "owns" their sandbox
        db.session.commit()

        login_user(team, remember=True)
        team.last_login = datetime.utcnow()
        team.last_seen = team.last_login
        db.session.commit()
        flash(f'Welcome to "{game.name}"! Your fund is raised — search for '
              f'companies to get started. When you finish a phase, mark it '
              f'complete on your dashboard and the simulation advances '
              f'automatically. Friends can join your game with code '
              f'{game.join_code} (until the first Deal Process runs).',
              'success')
        return redirect(url_for('dashboard'))

    return render_template('create_game.html', sectors=SECTORS,
                           fund_sizes=sorted(FUND_SIZE_PARTNERS))


@app.route('/join-game', methods=['GET', 'POST'])
def join_game():
    """Join an existing game with its join code — create your own team in it.
    Only allowed while the game is still at Year 1, Phase 1."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        if _rate_limited('join_game', 15, 600):
            flash('Too many attempts — please wait a few minutes.', 'warning')
            return redirect(url_for('join_game'))
        code = (request.form.get('join_code') or '').strip().upper()
        game = Game.query.filter_by(join_code=code).first() if code else None
        if not game:
            flash('No game found with that join code.', 'danger')
            return redirect(url_for('join_game'))
        if not game.is_joinable:
            flash(f'"{game.name}" is no longer accepting new teams — joining '
                  f'is only possible before the first Deal Process runs '
                  f'(Year 1, Phase 1).', 'warning')
            return redirect(url_for('join_game'))

        (firm_name, username, password, fund_type, sector_focus, fund_size,
         mgmt_fee, perf_fee) = _parse_team_signup_form()
        if not firm_name or not username or not password:
            flash('Firm name, username, and password are all required.', 'danger')
            return redirect(url_for('join_game'))
        if Team.query.filter_by(username=username).first():
            flash(f'Username "{username}" is already taken — pick another.',
                  'danger')
            return redirect(url_for('join_game'))

        team = _create_team_with_fund(game, firm_name, username, password,
                                      fund_type, sector_focus, fund_size,
                                      mgmt_fee, perf_fee)
        db.session.commit()

        login_user(team, remember=True)
        team.last_login = datetime.utcnow()
        team.last_seen = team.last_login
        db.session.commit()
        flash(f'Welcome to "{game.name}"! Your fund is raised — search for '
              f'companies to get started.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('join_game.html', sectors=SECTORS,
                           fund_sizes=sorted(FUND_SIZE_PARTNERS))


# ---------------------------------------------------------------------------
# Phase readiness / auto-advance helpers
# ---------------------------------------------------------------------------

def _team_roster(game):
    """Non-admin teams for this game, ordered stably for display."""
    return (Team.query
            .filter_by(game_id=game.id, is_admin=False)
            .order_by(Team.id)
            .all())


def _readiness(game):
    """(roster, ready_count, total) for the current phase. Each roster entry is
    a dict the dashboard/admin templates and the status API can all render."""
    teams = _team_roster(game)
    roster = [{'id': t.id, 'firm_name': t.firm_name,
               'ready': t.is_ready_for(game)} for t in teams]
    ready = sum(1 for r in roster if r['ready'])
    return roster, ready, len(roster)


def _clear_readiness(game):
    """Wipe every team's phase-complete signal (used on manual crank / reset)."""
    for t in _team_roster(game):
        t.ready_year = None
        t.ready_phase = None


def _run_current_phase_crank(game):
    """Run the crank for the game's current phase and return (message, category)
    for flashing. Caller must have confirmed game.status == 'active' and set any
    market condition beforehand. Shared by the admin Run-Process page and the
    automatic all-teams-ready trigger."""
    phase = game.current_phase
    game.status = 'in_crank'
    db.session.commit()
    if phase == 1:
        run_phase1_crank(game)
        return (f'Deal Process complete. Year {game.current_year} '
                f'Phase 2 is now open.'), 'success'
    run_phase2_crank(game)
    if game.status == 'completed':
        return (f"The fund's term has ended after Year {game.current_year}. "
                f'All remaining holdings were exited — the game is complete. '
                f'See the leaderboard for final results.'), 'success'
    return (f'Deal & Return Process complete. Year {game.current_year} '
            f'Phase 1 is now open.'), 'success'


def _maybe_auto_crank(game):
    """If auto-advance is on, the game is active, and every team has marked the
    current phase complete, run the crank. Returns (message, category) if it
    fired, else None. The status flip to 'in_crank' inside
    _run_current_phase_crank guards against a double-fire if two final 'ready'
    clicks race."""
    if not game.auto_advance or game.status != 'active':
        return None
    teams = _team_roster(game)
    if not teams or not all(t.is_ready_for(game) for t in teams):
        return None
    return _run_current_phase_crank(game)


def block_if_ready(f):
    """Guard a team action: once a team has marked the current phase complete,
    its mutating (POST) actions are frozen until it clicks Undo. Read (GET)
    requests still go through, so the team can look but not change anything."""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.method == 'POST' and not current_user.is_admin:
            game = Game.query.get(current_user.game_id)
            if game and current_user.is_ready_for(game):
                flash('You have marked this phase complete. Click "Undo" on '
                      'your dashboard if you want to make more changes.',
                      'warning')
                return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return wrapper


def current_game():
    """Resolve the game in context for the current request.

    - A team user is bound to its own game via `game_id`.
    - An admin manages one game at a time, selected via the session key
      'admin_game_id'. When nothing is selected yet, fall back to the first
      game so single-game behavior is unchanged until a game is picked.

    Returns None only when no games exist at all.
    """
    if current_user.is_authenticated and not current_user.is_admin:
        return Game.query.get(current_user.game_id)
    gid = session.get('admin_game_id')
    if gid is not None:
        g = Game.query.get(gid)
        if g is not None:
            return g
    return Game.query.first()


# Refresh last_seen at most this often — keeps the "last seen" signal live
# (the dashboard polls every few seconds) without a DB write per request.
LAST_SEEN_REFRESH = timedelta(minutes=3)


@app.before_request
def team_request_gate():
    if current_user.is_authenticated and not current_user.is_admin:
        # A team whose game was archived is signed out on its next request —
        # archiving a game retires its users along with it.
        game = Game.query.get(current_user.game_id)
        if game and game.is_archived:
            logout_user()
            flash('This game has been archived by the instructor — you have '
                  'been signed out.', 'info')
            return redirect(url_for('login'))
        now = datetime.utcnow()
        if (current_user.last_seen is None
                or now - current_user.last_seen > LAST_SEEN_REFRESH):
            current_user.last_seen = now
            db.session.commit()


@app.template_filter('time_ago')
def time_ago(dt):
    """Compact relative time ('3m ago', '2h ago', '5d ago') for roster views."""
    if not dt:
        return 'never'
    delta = datetime.utcnow() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60}m ago'
    if secs < 86400:
        return f'{secs // 3600}h ago'
    return f'{secs // 86400}d ago'


@app.context_processor
def inject_asset_version():
    """Version string for static assets (based on main.css mtime) so a new
    deploy busts the browser cache — otherwise clients keep stale CSS/JS."""
    try:
        v = int(os.path.getmtime(
            os.path.join(_BASE_DIR, 'static', 'css', 'main.css')))
    except OSError:
        v = 0
    return {'asset_version': v}


# ---------------------------------------------------------------------------
# Team Dashboard
# ---------------------------------------------------------------------------

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))
    game = Game.query.get(current_user.game_id)
    notifications = (Notification.query
                     .filter_by(team_id=current_user.id)
                     .order_by(Notification.created_at.desc())
                     .limit(10).all())
    active_deals = (Deal.query
                    .join(DealEquity, DealEquity.deal_id == Deal.id)
                    .filter(DealEquity.team_id == current_user.id,
                            Deal.status == 'active')
                    .all())
    # In-flight bids this year: where each term sheet stands in the deal
    # process (submitted / won lead / fill candidate / co-invest offer pending)
    bid_activity = (TermSheet.query
                    .filter_by(team_id=current_user.id,
                               game_year=game.current_year)
                    .filter(TermSheet.status.in_(
                        ['pending', 'lead', 'fill_offered', 'coinvest_offered']))
                    .all())
    # A won-lead term sheet keeps status 'lead' even after the deal closes, so
    # only prompt to finalize while the deal is still awaiting finalization.
    bid_activity = [ts for ts in bid_activity
                    if ts.status != 'lead'
                    or (ts.company.deal is not None
                        and ts.company.deal.status == 'pending_finalization')]
    # Holdings that have run out of cash and still need a decision — surfaced
    # prominently so the team can rescue them before they go bankrupt. Once the
    # lead has decided (invested clears in_distress, or chose to let it roll),
    # the company drops off this banner.
    distressed = [d for d in active_deals
                  if d.company.in_distress and not d.let_it_roll]
    # Recap of what happened LAST period (year N-1) to holdings that were in
    # distress: bankrupt / recovered / rescued-but-still-burning.
    prior_year = game.current_year - 1
    distress_recap = []
    if prior_year >= 1:
        recap_cos = (GameCompany.query
                     .join(Deal, Deal.company_id == GameCompany.id)
                     .join(DealEquity, DealEquity.deal_id == Deal.id)
                     .filter(DealEquity.team_id == current_user.id,
                             GameCompany.distress_resolution.isnot(None),
                             GameCompany.distress_resolution_year == prior_year)
                     .distinct()
                     .all())
        distress_recap = [{'name': c.name, 'outcome': c.distress_resolution}
                          for c in recap_cos]
    ret = team_simple_return(current_user, game)
    ready_roster, ready_count, total_teams = _readiness(game)
    return render_template('dashboard.html',
                           game=game,
                           notifications=notifications,
                           active_deals=active_deals,
                           distressed=distressed,
                           distress_recap=distress_recap,
                           bid_activity=bid_activity,
                           ret=ret,
                           ready_roster=ready_roster,
                           ready_count=ready_count,
                           total_teams=total_teams,
                           i_am_ready=current_user.is_ready_for(game))


@app.route('/ready', methods=['POST'])
@login_required
def mark_ready():
    """A team marks the current phase complete. If that was the last team and
    auto-advance is on, the crank fires immediately."""
    if current_user.is_admin:
        abort(403)
    game = Game.query.get(current_user.game_id)
    if not game or game.status != 'active':
        flash('This phase is not open for marking complete right now.', 'warning')
        return redirect(url_for('dashboard'))
    current_user.ready_year = game.current_year
    current_user.ready_phase = game.current_phase
    db.session.commit()
    fired = _maybe_auto_crank(game)
    if fired:
        msg, cat = fired
        flash('All teams marked this phase complete — ' + msg, cat)
    else:
        _, ready, total = _readiness(game)
        remaining = total - ready
        if game.auto_advance:
            flash(f"Marked complete. Waiting on {remaining} more "
                  f"team{'s' if remaining != 1 else ''} before the process "
                  f"runs automatically.", 'success')
        else:
            flash('Marked complete. (Auto-advance is off — your instructor '
                  'will run the process.)', 'success')
    return redirect(url_for('dashboard'))


@app.route('/ready/undo', methods=['POST'])
@login_required
def unmark_ready():
    """A team retracts its phase-complete signal (before the crank fires)."""
    if current_user.is_admin:
        abort(403)
    current_user.ready_year = None
    current_user.ready_phase = None
    db.session.commit()
    flash('You are no longer marked complete for this phase.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    Notification.query.filter_by(team_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return redirect(request.referrer or url_for('dashboard'))


# ---------------------------------------------------------------------------
# Firm Directory
# ---------------------------------------------------------------------------

@app.route('/firms')
@login_required
def firm_directory():
    game = Game.query.get(current_user.game_id)
    teams = (Team.query
             .filter_by(game_id=game.id, is_admin=False)
             .order_by(Team.firm_name)
             .all())
    return render_template('firms.html', teams=teams, game=game)


@app.route('/firm/edit', methods=['GET', 'POST'])
@login_required
def edit_firm():
    game = Game.query.get(current_user.game_id)
    if game.current_year > 1:
        flash('Firm profile can only be edited in Year 1.', 'warning')
        return redirect(url_for('firm_directory'))
    if request.method == 'POST':
        current_user.firm_name = request.form.get('firm_name', current_user.firm_name).strip()
        current_user.about_us = request.form.get('about_us', '').strip()
        db.session.commit()
        flash('Firm profile updated.', 'success')
        return redirect(url_for('firm_directory'))
    return render_template('edit_firm.html', game=game)


# ---------------------------------------------------------------------------
# Deal Flow (Phase 1)
# ---------------------------------------------------------------------------

@app.route('/dealflow')
@login_required
def dealflow():
    game = Game.query.get(current_user.game_id)
    rows = CompanySearch.query.filter_by(team_id=current_user.id).all()

    # Bids that didn't win the lead: rejected outright, still a live fill
    # candidate (fill_offered), or offered a fill slot the team wasn't brought
    # into / declined (fill_declined). Shown with the team's original terms so
    # they can review what they offered. (fill_accepted became a portfolio
    # holding, so it's excluded.)
    rejected_sheets = (TermSheet.query
                       .filter_by(team_id=current_user.id)
                       .filter(TermSheet.status.in_(
                           ['rejected', 'fill_offered', 'fill_declined']))
                       .order_by(TermSheet.game_year.desc(), TermSheet.id.desc())
                       .all())
    # A company on this list isn't a fresh deal-flow lead anymore; keep it out
    # of the watchlist/referral sections
    rejected_ids = {ts.company_id for ts in rejected_sheets}

    # Only companies still on the market belong in deal flow lists —
    # funded ones live in the owner's Portfolio instead
    watchlist_ids = [r.company_id for r in rows
                     if r.on_watchlist and r.company_id not in rejected_ids]
    watchlist_companies = (GameCompany.query.filter(
        GameCompany.id.in_(watchlist_ids),
        GameCompany.status == 'available',
        GameCompany.year_available == game.current_year).all()
        if watchlist_ids else [])

    # Inbound referrals not yet promoted to the watchlist
    referral_map = {r.company_id: r for r in rows
                    if not r.on_watchlist and not r.found_by_search
                    and r.company_id not in rejected_ids}
    referral_companies = (GameCompany.query.filter(
        GameCompany.id.in_(list(referral_map)),
        GameCompany.status == 'available',
        GameCompany.year_available == game.current_year).all()
        if referral_map else [])

    return render_template('dealflow/index.html',
                           game=game,
                           watchlist_companies=watchlist_companies,
                           referral_companies=referral_companies,
                           referral_map=referral_map,
                           rejected_sheets=rejected_sheets)


MAX_SEARCH_RESULTS = 15  # cap on new companies found per search


@app.route('/dealflow/search', methods=['GET', 'POST'])
@login_required
@block_if_ready
def search_companies():
    game = Game.query.get(current_user.game_id)
    if game.current_phase != 1:
        flash('Company search is only available in Phase 1.', 'warning')
        return redirect(url_for('dealflow'))

    all_sectors = db.session.query(GameCompany.sector).filter_by(
        game_id=game.id).distinct().all()
    sectors = sorted(set(s[0].split('/')[0].strip() for s in all_sectors))

    results = None  # None = no search submitted yet; searches are free/unlimited
    form_values = {}
    if request.method == 'POST':
        results = []
        sector_filter = request.form.get('sector', '')
        stage_filter = request.form.get('stage', '')
        min_deal_size = request.form.get('min_deal_size', '')
        max_deal_size = request.form.get('max_deal_size', '')
        form_values = {'sector': sector_filter, 'stage': stage_filter,
                       'min_deal_size': min_deal_size, 'max_deal_size': max_deal_size}

        # Build query — companies are only on the market in their designated year
        query = GameCompany.query.filter_by(
            game_id=game.id, status='available').filter(
            GameCompany.year_available == game.current_year)

        if sector_filter:
            query = query.filter(GameCompany.sector == sector_filter)
        if stage_filter:
            query = query.filter(GameCompany.stage == stage_filter)
        # Deal size = funds wanted for growth-stage companies, but the whole
        # company's asking valuation for mature ones (you buy the business)
        if min_deal_size:
            try:
                v = float(min_deal_size)
                query = query.filter(or_(
                    and_(GameCompany.stage == 'mature',
                         GameCompany.initial_val_ask >= v),
                    and_(GameCompany.stage != 'mature',
                         GameCompany.capital_requested >= v)))
            except ValueError:
                pass
        if max_deal_size:
            try:
                v = float(max_deal_size)
                query = query.filter(or_(
                    and_(GameCompany.stage == 'mature',
                         GameCompany.initial_val_ask <= v),
                    and_(GameCompany.stage != 'mature',
                         GameCompany.capital_requested <= v)))
            except ValueError:
                pass

        companies = query.all()

        # Companies already on the watchlist or in referrals don't reappear;
        # everything else can resurface in later searches (results are not saved)
        known_ids = set(
            s.company_id for s in CompanySearch.query.filter_by(
                team_id=current_user.id).all())

        results = [c for c in companies if c.id not in known_ids]
        if len(results) > MAX_SEARCH_RESULTS:
            results = random.sample(results, MAX_SEARCH_RESULTS)
            flash(f'Your search matched more companies than your analysts '
                  f'could evaluate — showing {MAX_SEARCH_RESULTS}. '
                  f'Narrow your criteria to see specific targets.', 'info')

        # Shamrock: small chance of finding one extra company outside criteria
        all_available = GameCompany.query.filter_by(
            game_id=game.id, status='available').filter(
            GameCompany.year_available == game.current_year).all()
        unseen = [c for c in all_available
                  if c.id not in known_ids and c not in results]
        if unseen and random.random() < 0.3:
            bonus = random.choice(unseen)
            results.append(bonus)
            flash(f'Your analysts stumbled upon an additional opportunity: {bonus.name}!', 'info')

        # Grant view access to this result set until the next search
        session['last_search_ids'] = [c.id for c in results]
        session['last_search_filters'] = form_values

        if not results:
            flash('No new companies found matching your criteria. Try relaxing your search parameters.', 'info')

    elif request.args.get('restore') and session.get('last_search_ids'):
        # Re-show the previous search results (e.g. "Back to Search Results"
        # from a company detail page). Drop any that are no longer on market.
        ids = session['last_search_ids']
        found = {c.id: c for c in GameCompany.query.filter(
            GameCompany.id.in_(ids), GameCompany.game_id == game.id,
            GameCompany.status == 'available',
            GameCompany.year_available == game.current_year).all()}
        results = [found[i] for i in ids if i in found]
        form_values = session.get('last_search_filters', {})

    return render_template('dealflow/search.html',
                           game=game,
                           results=results,
                           form_values=form_values,
                           sectors=sectors)


@app.route('/company/<int:company_id>')
@login_required
def company_detail(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()

    # Viewable if on the watchlist / referred, in the latest search results,
    # or by the admin
    search_record = CompanySearch.query.filter_by(
        team_id=current_user.id, company_id=company_id).first()
    in_last_search = company_id in session.get('last_search_ids', [])
    if not search_record and not in_last_search and not current_user.is_admin:
        abort(403)

    # Check if team already submitted a term sheet this year
    existing_ts = TermSheet.query.filter_by(
        team_id=current_user.id, company_id=company_id,
        game_year=game.current_year).first()

    teams = Team.query.filter_by(game_id=game.id, is_admin=False).all()
    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all()

    db.session.refresh(company)
    return_assumption = ReturnAssumption.query.filter_by(
        sector=company.sector, stage=company.stage).first()
    return render_template('dealflow/company_detail.html',
                           game=game,
                           company=company,
                           existing_ts=existing_ts,
                           teams=teams,
                           funds=funds,
                           from_search=(request.args.get('src') == 'search'),
                           on_watchlist=bool(search_record and search_record.on_watchlist),
                           return_assumption=return_assumption)


@app.route('/company/<int:company_id>/watchlist', methods=['POST'])
@login_required
def add_to_watchlist(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    row = CompanySearch.query.filter_by(
        team_id=current_user.id, company_id=company_id).first()
    if row:
        row.on_watchlist = True
    else:
        db.session.add(CompanySearch(
            team_id=current_user.id, company_id=company_id,
            game_year=game.current_year, found_by_search=True,
            on_watchlist=True))
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True, 'company': company.name})
    flash(f'{company.name} added to your watchlist.', 'success')
    return redirect(request.referrer or url_for('dealflow'))


@app.route('/company/<int:company_id>/watchlist/remove', methods=['POST'])
@login_required
def remove_from_watchlist(company_id):
    row = CompanySearch.query.filter_by(
        team_id=current_user.id, company_id=company_id).first_or_404()
    if row.found_by_search:
        db.session.delete(row)       # forget it; future searches may resurface it
    else:
        row.on_watchlist = False     # referral: demote back to the referrals list
    db.session.commit()
    flash('Removed from watchlist.', 'info')
    return redirect(url_for('dealflow'))


@app.route('/company/<int:company_id>/refer', methods=['POST'])
@login_required
@block_if_ready
def refer_company(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    target_team_id = request.form.get('team_id', type=int)
    target_team = Team.query.get_or_404(target_team_id)

    if game.current_phase != 1:
        flash('Referrals are only available in Phase 1.', 'warning')
        return redirect(url_for('company_detail', company_id=company_id))

    # Check not already referred
    existing = CompanySearch.query.filter_by(
        team_id=target_team_id, company_id=company_id,
        game_year=game.current_year).first()
    if existing:
        flash(f'{target_team.firm_name} already has access to {company.name}.', 'info')
        return redirect(url_for('company_detail', company_id=company_id))

    cs = CompanySearch(
        team_id=target_team_id,
        company_id=company_id,
        game_year=game.current_year,
        found_by_search=False,
        referred_by_team_id=current_user.id
    )
    db.session.add(cs)
    _notify(target_team_id,
            f'{current_user.firm_name} has referred {company.name} to you.',
            'referral', company_id)
    db.session.commit()
    flash(f'{company.name} referred to {target_team.firm_name}.', 'success')
    return redirect(url_for('company_detail', company_id=company_id))


# Buyout leverage cap: debt may not exceed this share of the purchase valuation.
MAX_DEBT_PCT = 0.60
# Flat cost ($M) a fund pays to replace a portfolio company's management team.
CHANGE_MGMT_COST = 5.0
# Probability the rerolled management team lands at weak or average; the chance
# of strong is whatever's left (1 - weak - average), so the three sum to 100%.
CHANGE_MGMT_P_WEAK = 0.25
CHANGE_MGMT_P_AVERAGE = 0.50


@app.route('/company/<int:company_id>/termsheet', methods=['GET', 'POST'])
@login_required
@block_if_ready
def create_term_sheet(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()

    if game.current_phase != 1:
        flash('Term sheets can only be submitted in Phase 1.', 'warning')
        return redirect(url_for('company_detail', company_id=company_id))

    if company.status != 'available':
        flash('This company is no longer available for new term sheets.', 'warning')
        return redirect(url_for('company_detail', company_id=company_id))

    if company.year_available != game.current_year:
        flash(f'{company.name} is not on the market this year — its window '
              f'was Year {company.year_available}.', 'warning')
        return redirect(url_for('company_detail', company_id=company_id))

    # Mandate is NOT checked here: any team may submit, but the company
    # rejects off-mandate term sheets at the Deal Process
    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all()
    teams = (Team.query.filter_by(game_id=game.id, is_admin=False)
             .filter(Team.id != current_user.id).all())

    if request.method == 'POST':
        try:
            pre_money = float(request.form['pre_money_valuation'])
            if company.stage == 'mature':
                # Buyout: the student chooses how much DEBT to load on; the
                # equity check is the consequence. Sellers cash out 100% (no
                # rollover); the management pool is the only non-buyer equity.
                # equity = (1 - pool)(price - debt), so the buyer's stake is
                # worth their full check and debt is recovered at finalize.
                debt_amount = float(request.form.get('debt_amount') or 0)
                pool = company.management_option_pct or 0.0
                total_investment = ((1 - pool) * (pre_money - debt_amount)
                                    if pool < 1 else (pre_money - debt_amount))
                rolled_val = 0.0
            else:
                # Venture: founders roll over ALL their equity (no secondary).
                # Founder ownership is just their pre-money stake diluted by the
                # new money = pre / post-money; not a choice the team makes.
                total_investment = float(request.form['total_investment'])
                post = pre_money + total_investment
                rolled_val = (pre_money / post) if post > 0 else 0.0
            rolled_min = rolled_max = rolled_val
            fund_id = int(request.form['fund_id'])
            liq_pref = int(request.form.get('liquidation_preference', 1))
            participation = 'participation' in request.form
            anti_dilution = request.form.get('anti_dilution', 'none')
            willing_fill = 'willing_to_fill' in request.form
            # Max co-investment removed — fill investors only toggle willingness;
            # the lead proposes any amount and they accept/reject. Column kept at 0.
            max_fill = 0.0
            min_rep = float(request.form.get('min_lead_reputation') or 0)
            # Co-investment is handled solely through the fill flow at
            # finalization; term sheets are always solo at submission.

            # Validate restrictions for mature/early_revenue
            if not company.allows_aggressive_terms:
                if liq_pref > 1:
                    flash('Liquidation preference > 1x not allowed for this company stage.', 'danger')
                    return redirect(request.url)
                if participation:
                    flash('Participation not allowed for this company stage.', 'danger')
                    return redirect(request.url)
                if anti_dilution in ('weighted', 'full_ratchet'):
                    flash('Anti-dilution provisions not allowed for this company stage.', 'danger')
                    return redirect(request.url)

            # Buyout debt is capped at MAX_DEBT_PCT of the purchase valuation.
            # (Enforced both ways: too much debt, or a price low enough that the
            # chosen debt tops 60% of it, is rejected.)
            if company.stage == 'mature':
                max_debt = MAX_DEBT_PCT * pre_money
                if debt_amount < 0 or debt_amount > max_debt + 1e-6:
                    flash(f'Debt can be at most {MAX_DEBT_PCT * 100:.0f}% of the '
                          f'${pre_money:,.1f}M purchase valuation (${max_debt:,.1f}M). '
                          f'Lower the debt or raise the price.', 'danger')
                    return redirect(request.url)

            # Capital discipline: a team can't submit term sheets that together
            # commit more equity than the fund has available to invest. Sum the
            # fund's other outstanding bids this round + this one vs. its cash.
            fund = Fund.query.get(fund_id)
            already_committed = sum(
                t.total_investment for t in TermSheet.query.filter_by(
                    team_id=current_user.id, fund_id=fund_id,
                    game_year=game.current_year, status='pending').all())
            available = fund.available_capital if fund else 0.0
            remaining = available - already_committed
            if total_investment > remaining + 1e-6:
                msg = (f"This ${total_investment:,.1f}M term sheet is more than your "
                       f"fund can invest. {fund.name if fund else 'Your fund'} has "
                       f"${available:,.1f}M available")
                if already_committed > 1e-6:
                    msg += (f", and you've already committed ${already_committed:,.1f}M "
                            f"in term sheets this round — only ${max(0.0, remaining):,.1f}M left")
                msg += ". Lower this bid to fit your capital."
                flash(msg, 'danger')
                return redirect(request.url)

            ts = TermSheet(
                team_id=current_user.id,
                company_id=company_id,
                fund_id=fund_id,
                game_year=game.current_year,
                pre_money_valuation=pre_money,
                total_investment=total_investment,
                rolled_equity_min=rolled_min,
                rolled_equity_max=rolled_max,
                liquidation_preference=liq_pref,
                participation=participation,
                anti_dilution=anti_dilution,
                willing_to_fill=willing_fill,
                max_fill_equity=max_fill,
                min_lead_reputation=min_rep,
                term_sheet_type='solo',
            )
            db.session.add(ts)
            db.session.flush()

            # Keep active deals visible: auto-add the company to the watchlist
            cs = CompanySearch.query.filter_by(
                team_id=current_user.id, company_id=company_id).first()
            if cs:
                cs.on_watchlist = True
            else:
                db.session.add(CompanySearch(
                    team_id=current_user.id, company_id=company_id,
                    game_year=game.current_year, found_by_search=True,
                    on_watchlist=True))

            db.session.commit()
            flash('Term sheet submitted successfully!', 'success')
            return redirect(url_for('timeline'))

        except (ValueError, KeyError) as e:
            flash(f'Error submitting term sheet: {e}', 'danger')

    return render_template('dealflow/term_sheet.html',
                           game=game,
                           company=company,
                           funds=funds,
                           teams=teams)


# ---------------------------------------------------------------------------
# Timeline (Phase 2 deal finalization + syndicate approvals)
# ---------------------------------------------------------------------------

def _implied_ownership_pct(company, pre, inv):
    """Fully-diluted ownership a lead would hold at these terms if it funds solo.
    Mirrors finalize_deal: the option pool is carved from the founder/seller
    side, so it only dilutes the buyer if it exceeds that side."""
    pool_pct = (company.management_option_pct or 0) * 100.0
    if company.stage == 'mature':
        # Buyout: sellers cash out 100%, buyer owns everything but the pool.
        return max(0.0, 100.0 - pool_pct)
    pre = pre or 0.0
    inv = inv or 0.0
    post = pre + inv
    if post <= 0:
        return 0.0
    rolled_pct = pre / post * 100.0
    return max(0.0, (100.0 - rolled_pct) - max(0.0, pool_pct - rolled_pct))


@app.route('/timeline')
@login_required
def timeline():
    game = Game.query.get(current_user.game_id)

    # My term sheets this year
    my_ts = (TermSheet.query
             .filter_by(team_id=current_user.id, game_year=game.current_year)
             .all())

    # Co-investment offers awaiting my accept/reject (I'm the invited team)
    coinvest_offers = (TermSheet.query
                       .filter_by(team_id=current_user.id,
                                  game_year=game.current_year,
                                  status='coinvest_offered')
                       .all())

    # Deals I lead that are waiting on co-investors before they close
    awaiting_coinvest = (Deal.query
                         .filter_by(lead_team_id=current_user.id,
                                    game_year=game.current_year,
                                    status='pending_coinvest')
                         .all())

    # My funds, keyed by id, so co-invest offers can show available capital
    my_funds = {f.id: f for f in
                Fund.query.filter_by(team_id=current_user.id, is_active=True).all()}

    # Per term sheet: implied ownership at these terms, and — for a bid that won
    # the lead — the pending deal to finalize plus how many fill investors are
    # interested (so the term-sheet row can carry the Finalize action itself).
    ts_info = {}
    for ts in my_ts:
        deal_id, fills = None, 0
        if ts.status == 'lead':
            deal = (Deal.query
                    .filter_by(company_id=ts.company_id, game_year=game.current_year,
                               lead_team_id=current_user.id,
                               status='pending_finalization')
                    .first())
            if deal:
                deal_id = deal.id
                fills = (TermSheet.query
                         .filter_by(company_id=ts.company_id,
                                    game_year=game.current_year, status='fill_offered')
                         .filter(TermSheet.team_id != current_user.id)
                         .count())
        ts_info[ts.id] = {
            'deal_id': deal_id,
            'fills': fills,
            'ownership': _implied_ownership_pct(
                ts.company, ts.pre_money_valuation, ts.total_investment),
        }

    return render_template('timeline.html',
                           game=game,
                           my_term_sheets=my_ts,
                           coinvest_offers=coinvest_offers,
                           awaiting_coinvest=awaiting_coinvest,
                           my_funds=my_funds,
                           ts_info=ts_info)


@app.route('/timeline/coinvest/<int:ts_id>', methods=['POST'])
@login_required
@block_if_ready
def respond_coinvest(ts_id):
    ts = TermSheet.query.get_or_404(ts_id)
    if ts.team_id != current_user.id:
        abort(403)
    if ts.status != 'coinvest_offered':
        flash('This co-investment offer is no longer open.', 'info')
        return redirect(url_for('timeline'))

    company = GameCompany.query.get(ts.company_id)
    deal = Deal.query.filter_by(company_id=ts.company_id,
                                game_year=ts.game_year,
                                status='pending_coinvest').first()
    decision = request.form.get('decision', 'accept')
    amount = ts.proposed_coinvest_amount or 0.0

    if decision == 'accept':
        fund = Fund.query.get(ts.fund_id)
        if not fund or fund.available_capital + 1e-6 < amount:
            avail = fund.available_capital if fund else 0.0
            flash(f'Your fund only has ${avail:,.1f}M available — not enough to '
                  f'cover the ${amount:,.1f}M co-investment.', 'danger')
            return redirect(url_for('timeline'))
        ts.status = 'fill_accepted'
        _notify(deal.lead_team_id,
                f'{current_user.firm_name} accepted your ${amount:,.1f}M '
                f'co-investment offer on {company.name}.',
                'fill_offered', company.id)
        flash(f'You accepted the ${amount:,.1f}M co-investment in {company.name}.',
              'success')
    else:
        ts.status = 'fill_declined'
        ts.proposed_coinvest_amount = None
        ts.rejection_reason = (
            f"You declined the ${amount:,.1f}M co-investment offer on "
            f"{company.name}.")
        _notify(deal.lead_team_id,
                f'{current_user.firm_name} declined your co-investment offer on '
                f'{company.name}; your fund will backstop that slice.',
                'deal_lost', company.id)
        flash(f'You declined the co-investment in {company.name}.', 'info')

    db.session.commit()

    # If every invited co-investor has now responded, close the deal
    if deal:
        outstanding = (TermSheet.query
                       .filter_by(company_id=deal.company_id,
                                  game_year=deal.game_year,
                                  status='coinvest_offered')
                       .filter(TermSheet.team_id != deal.lead_team_id)
                       .count())
        if outstanding == 0:
            close_deal_with_coinvestors(deal)

    return redirect(url_for('timeline'))


@app.route('/deal/<int:deal_id>/finalize', methods=['GET', 'POST'])
@login_required
@block_if_ready
def finalize_deal_route(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    game = Game.query.get(current_user.game_id)

    if deal.lead_team_id != current_user.id:
        abort(403)
    if deal.status != 'pending_finalization':
        flash('This deal has already been finalized.', 'info')
        return redirect(url_for('timeline'))

    company = deal.company
    lead_ts = deal.lead_term_sheet

    # Find compatible fill term sheets
    fill_offers = (TermSheet.query
                   .filter_by(company_id=company.id,
                              game_year=game.current_year)
                   .filter(TermSheet.status.in_(['fill_offered', 'fill_accepted']))
                   .filter(TermSheet.team_id != current_user.id)
                   .all())

    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all()

    if request.method == 'POST':
        # Dropping an accepted deal isn't allowed: once a team's term sheet is
        # accepted, the deal is committed and must be finalized. (Drop may be
        # reintroduced in the future.)
        try:
            # All deal economics are LOCKED from the accepted term sheet;
            # finalization only decides who is invited to fund the equity.
            total_equity = lead_ts.total_investment

            # Build the lead's co-investment proposals. The lead proposes any
            # amount per invited fill; total can't exceed the committed equity.
            proposals = []
            proposed_total = 0.0
            selected_fills = request.form.getlist('selected_fills')
            for fill_ts_id in selected_fills:
                fts = TermSheet.query.get(int(fill_ts_id))
                if not fts or fts.status != 'fill_offered':
                    continue
                amount = float(request.form.get(f'fill_equity_{fill_ts_id}') or 0)
                if amount <= 0:
                    continue
                proposed_total += amount
                proposals.append((fts, amount))

            if proposed_total > total_equity + 1e-6:
                flash(f'Proposed co-investments cannot exceed the ${total_equity:.1f}M '
                      f'equity committed in your term sheet.', 'danger')
                return redirect(request.url)

            # Fills the lead did not invite are out
            for fts in fill_offers:
                if str(fts.id) not in selected_fills and fts.status == 'fill_offered':
                    fts.status = 'fill_declined'
                    fts.rejection_reason = (
                        f"{current_user.firm_name} won the lead and chose not to "
                        f"bring you in as a co-investor.")
                    _notify(fts.team_id,
                            f'You were not invited to co-invest in the final '
                            f'{company.name} deal.',
                            'deal_lost', company.id)

            if not proposals:
                # Solo close: nobody to wait on, finalize immediately
                stakes = [{'team_id': current_user.id, 'fund_id': lead_ts.fund_id,
                           'equity_invested': total_equity}]
                _, rolled_pct, debt_amount, debt_rate, mgmt_options = (
                    locked_deal_economics(deal))
                finalize_deal(deal, lead_ts.pre_money_valuation, stakes,
                              rolled_pct, debt_amount, debt_rate, mgmt_options)
                current_user.reputation = min(5.0, current_user.reputation + 0.2)
                db.session.commit()
                flash(f'Deal on {company.name} finalized successfully!', 'success')
                return redirect(url_for('timeline'))

            # Co-investors invited: send offers and wait for their responses
            for fts, amount in proposals:
                fts.status = 'coinvest_offered'
                fts.proposed_coinvest_amount = amount
                _notify(fts.team_id,
                        f'{current_user.firm_name} invites you to co-invest '
                        f'${amount:,.1f}M in {company.name} at the finalized terms. '
                        f'Review and accept or reject on your Timeline.',
                        'fill_offered', company.id)
            deal.status = 'pending_coinvest'
            db.session.commit()
            flash(f'Co-investment offers sent for {company.name}. The deal will '
                  f'close once the invited teams respond.', 'success')
            return redirect(url_for('timeline'))

        except (ValueError, KeyError) as e:
            flash(f'Error finalizing deal: {e}', 'danger')

    return render_template('phase2/finalize_deal.html',
                           game=game,
                           deal=deal,
                           company=company,
                           lead_ts=lead_ts,
                           debt_rate=DEBT_INTEREST_RATE,
                           fill_offers=fill_offers,
                           funds=funds)


# ---------------------------------------------------------------------------
# Portfolio Management
# ---------------------------------------------------------------------------

@app.route('/portfolio')
@login_required
def portfolio():
    game = Game.query.get(current_user.game_id)
    stakes = (DealEquity.query
              .filter_by(team_id=current_user.id)
              .join(Deal, DealEquity.deal_id == Deal.id)
              .filter(Deal.status.in_(['active', 'pending_finalization']))
              .all())
    exited_stakes = (DealEquity.query
                     .filter_by(team_id=current_user.id)
                     .join(Deal, DealEquity.deal_id == Deal.id)
                     .filter(Deal.status.in_(['liquidated', 'bankrupt']))
                     .all())
    liquidated = []
    for s in exited_stakes:
        comp = s.deal.company
        proceeds_txs = (FundTransaction.query
                        .filter_by(fund_id=s.fund_id,
                                   transaction_type='liquidation_proceeds',
                                   company_id=comp.id)
                        .all())
        liquidated.append({
            'company_id': comp.id,
            'company_name': comp.name,
            'sector': comp.sector,
            'exit_year': proceeds_txs[0].game_year if proceeds_txs else s.deal.game_year,
            'ownership_pct': s.ownership_pct,
            'equity_invested': s.equity_invested,
            'proceeds': sum(t.amount for t in proceeds_txs),
        })
    return render_template('portfolio/index.html',
                           game=game,
                           stakes=stakes,
                           liquidated=liquidated)


# Dividends let the lead pull cash up from a cash-flow-positive portfolio company
# to its fund (pro-rata to ownership) — a key way for GPs to raise cash to cover
# management fees. Capped at MAX_DIVIDEND_PCT of the company's cash per dividend.
DIVIDENDS_ENABLED = True
MAX_DIVIDEND_PCT = 0.20


@app.route('/portfolio/company/<int:company_id>')
@login_required
def portfolio_company(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal
    if not deal:
        abort(404)

    # Verify team has a stake
    stake = DealEquity.query.filter_by(
        deal_id=deal.id, team_id=current_user.id).first()
    if not stake and not current_user.is_admin:
        abort(403)

    is_lead = (deal.lead_team_id == current_user.id)
    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all() if is_lead else []
    all_teams = Team.query.filter_by(game_id=game.id, is_admin=False).all() if is_lead else []
    waterfall = exit_waterfall(deal) if deal.status == 'liquidated' else None

    # Year-by-year valuation bridge from the funded valuation, with the
    # realized annual return at each crank
    return_assumption = ReturnAssumption.query.filter_by(
        sector=company.sector, stage=company.stage).first()
    val_history = []
    if company.year_funded and company.funded_valuation:
        prev = company.funded_valuation
        for y in range(company.year_funded, GameCompany.MAX_TRACKED_YEARS + 1):
            v = company.get_year_val(y)
            if v is None:
                continue
            mgmt_fee = company.get_year_mgmt_fee(y)
            followon = company.get_year_followon(y)
            dividend = company.get_year_dividend(y)
            # The market's dollar contribution is whatever isn't explained by the
            # recorded cash events, so the bridge always ties to the stored mark:
            #   opening + market_gain + follow-on - mgmt - dividend = closing.
            # (For a cranked year this equals opening x (roll - 1) exactly.)
            market_gain = v - prev - followon + mgmt_fee + dividend
            val_history.append({'year': y, 'value': v, 'opening': prev,
                                'market_gain': market_gain,
                                'mgmt_fee': mgmt_fee,
                                'followon': followon,
                                'dividend': dividend})
            prev = v
    val_has_followons = any(r['followon'] for r in val_history)
    val_has_mgmt = any(r['mgmt_fee'] for r in val_history)
    val_has_dividends = any(r['dividend'] for r in val_history)

    # Cash register: a running balance by year with a UNIFIED column set so a
    # company's whole history reads consistently — including a venture company
    # that burned cash for years and then turned profitable. Each year is shown
    # in EBITDA mode (if EBITDA was recorded that year) or burn mode (if a burn
    # was recorded); follow-on injections and mgmt-change fees adjust either.
    # Clamped at $0 on cash exhaustion (flags distress).
    is_buyout = company.stage == 'mature'
    cash_register = []
    register_start = 0.0
    if company.year_funded and company.funded_valuation:
        register_start = 0.0 if is_buyout else \
            (deal.total_equity_invested or 0.0) + (deal.debt_amount or 0.0)
        annual_interest = (company.debt_outstanding or 0.0) * (company.debt_interest_rate or 0.0)
        bal = register_start
        for row in val_history:
            y = row['year']
            opening = bal
            ebitda_y = company.get_year_ebitda(y)
            burn_y = company.get_year_burn(y)
            if ebitda_y is None and burn_y is None:
                # Legacy year with no recorded flow: infer from current state.
                if is_buyout or company.turned_profitable:
                    ebitda_y = company.ltm_ebitda or 0.0
                else:
                    burn_y = company.annual_burn_rate or 0.0
            # A burning year always records a burn; a profitable/mature year
            # records EBITDA and no burn — so a recorded burn wins the mode
            # (ignoring any stale EBITDA from a prior model).
            if burn_y is not None:
                # Burning year (venture, pre-profit).
                op = -(burn_y or 0.0)
                rowdata = {'ebitda': None, 'cash': None, 'burn': burn_y,
                           'interest': None}
            else:
                # Profitable year: EBITDA converts to cash; debt interest paid.
                cash_y = ebitda_to_cash(ebitda_y)
                interest_y = annual_interest
                op = cash_y - interest_y
                rowdata = {'ebitda': ebitda_y, 'cash': cash_y, 'burn': None,
                           'interest': interest_y}
            # Follow-on injections add cash; management-change fees and
            # dividends subtract it.
            followon_y = company.get_year_followon(y)
            mgmt_fee_y = company.get_year_mgmt_fee(y)
            dividend_y = company.get_year_dividend(y)
            raw_close = opening + op + followon_y - mgmt_fee_y - dividend_y
            rowdata['followon'] = followon_y
            rowdata['mgmt_fee'] = mgmt_fee_y
            rowdata['dividend'] = dividend_y
            closing = max(0.0, raw_close)
            rowdata.update({'year': y, 'opening': opening,
                            'closing': closing, 'distressed': raw_close < 0})
            cash_register.append(rowdata)
            bal = closing

        # Follow-on injections and management-change fees hit cash immediately,
        # but the current year may not be cranked yet (no valuation row). Add an
        # "in progress" row for any such year so the running balance matches
        # actual cash on hand.
        covered = {r['year'] for r in cash_register}
        for y in range(company.year_funded, game.current_year + 1):
            followon_y = company.get_year_followon(y)
            fee_y = company.get_year_mgmt_fee(y)
            dividend_y = company.get_year_dividend(y)
            if y in covered or (not fee_y and not followon_y and not dividend_y):
                continue
            opening = bal
            raw_close = opening + followon_y - fee_y - dividend_y
            closing = max(0.0, raw_close)
            cash_register.append({'year': y, 'opening': opening,
                                  'ebitda': None, 'cash': None, 'burn': None,
                                  'interest': None, 'followon': followon_y,
                                  'mgmt_fee': fee_y, 'dividend': dividend_y,
                                  'closing': closing,
                                  'distressed': raw_close < 0, 'pending': True})
            bal = closing
    register_has_mgmt_fees = any(r.get('mgmt_fee') for r in cash_register)
    register_has_followons = any(r.get('followon') for r in cash_register)
    register_has_dividends = any(r.get('dividend') for r in cash_register)

    return render_template('portfolio/company.html',
                           game=game,
                           company=company,
                           deal=deal,
                           stake=stake,
                           waterfall=waterfall,
                           return_assumption=return_assumption,
                           val_history=val_history,
                           val_has_followons=val_has_followons,
                           val_has_mgmt=val_has_mgmt,
                           val_has_dividends=val_has_dividends,
                           cash_register=cash_register,
                           register_start=register_start,
                           register_has_mgmt_fees=register_has_mgmt_fees,
                           register_has_followons=register_has_followons,
                           register_has_dividends=register_has_dividends,
                           is_lead=is_lead,
                           funds=funds,
                           dividends_enabled=DIVIDENDS_ENABLED,
                           max_dividend_pct=MAX_DIVIDEND_PCT,
                           change_mgmt_cost=CHANGE_MGMT_COST,
                           all_teams=all_teams)


@app.route('/portfolio/company/<int:company_id>/change_mgmt', methods=['POST'])
@login_required
@block_if_ready
def change_management(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)
    if company.year_funded >= game.current_year:
        flash('Management can only be changed after at least 1 year in portfolio.', 'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))

    cost = CHANGE_MGMT_COST

    # The fee is paid out of the company's own cash (not the fund's capital).
    if (company.company_funds or 0) < cost:
        flash(f"{company.name} doesn't have enough cash to cover the "
              f"${cost:,.1f}M management-change fee "
              f"(cash on hand: ${company.company_funds or 0:,.1f}M).", 'danger')
        return redirect(url_for('portfolio_company', company_id=company_id))

    company.company_funds -= cost
    # Record the fee in this year. The valuation incorporates the company's cash,
    # so the mark drops by the same amount — but that reduction is applied to
    # THIS year's mark at the next Deal & Return Process (the crank), so the
    # valuation history ties out (prior mark x roll - fee = new mark). The cash
    # leaves immediately; the mark is re-struck when results are processed.
    company.add_year_mgmt_fee(game.current_year, cost)

    # Randomly assign new management quality using the configured probabilities.
    # Strong is the remainder so the three odds always sum to 100%.
    p_strong = max(0.0, 1.0 - CHANGE_MGMT_P_WEAK - CHANGE_MGMT_P_AVERAGE)
    company.management_quality = random.choices(
        ['weak', 'average', 'strong'],
        weights=[CHANGE_MGMT_P_WEAK, CHANGE_MGMT_P_AVERAGE, p_strong])[0]
    # Slight reputation hit for instability
    current_user.reputation = max(1.0, current_user.reputation - 0.1)
    db.session.commit()
    flash(f"Management team replaced at {company.name}. ${cost:,.1f}M paid "
          f"from the company's cash; its valuation drops by the same amount "
          f"when this year's results are processed. "
          f"New management quality: {company.management_quality}.", 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


@app.route('/portfolio/company/<int:company_id>/dividend', methods=['POST'])
@login_required
@block_if_ready
def issue_dividend(company_id):
    if not DIVIDENDS_ENABLED:
        abort(404)
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)
    if company.net_annual_cash_flow <= 0:
        flash("This company isn't generating positive cash flow (its EBITDA "
              "doesn't cover its debt interest), so it can't pay a dividend.",
              'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))

    # Max dividend = MAX_DIVIDEND_PCT of company funds
    max_div = company.company_funds * MAX_DIVIDEND_PCT
    amount = float(request.form.get('amount', 0))
    if amount <= 0 or amount > max_div:
        flash(f'Dividend amount must be between $0 and ${max_div:.2f}M.', 'danger')
        return redirect(url_for('portfolio_company', company_id=company_id))

    company.company_funds -= amount
    # The valuation incorporates the company's cash, so paying it out lowers the
    # mark by the same amount (mirror of the management-change fee) — otherwise a
    # team could dividend out cash and still collect the full mark at exit. That
    # reduction is applied to this year's mark at the next Deal & Return Process
    # so the valuation history ties out. Record it for both itemized tables.
    company.add_year_dividend(game.current_year, amount)

    # Distribute to each investor proportionally
    total_investor_pct = sum(s.ownership_pct for s in deal.equity_stakes)
    for stake in deal.equity_stakes:
        share = amount * (stake.ownership_pct / total_investor_pct) if total_investor_pct > 0 else 0
        fund = Fund.query.get(stake.fund_id)
        fund.available_capital += share
        _record_transaction(stake.fund_id, 'dividend_received', share,
                            f'Dividend from {company.name}', game.current_year, company.id)
        _notify(stake.team_id,
                f'{company.name} paid a dividend. Your share: ${share:.2f}M.',
                'dividend', company.id)

    db.session.commit()
    flash(f'Dividend of ${amount:.2f}M issued from {company.name}.', 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


@app.route('/portfolio/company/<int:company_id>/liquidate', methods=['POST'])
@login_required
@block_if_ready
def mark_liquidation(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)

    # A blank reserve-price field means "no floor" — treat it as 0.
    reserve_price = float(request.form.get('reserve_price', 0) or 0)
    deal.marked_for_liquidation = True
    deal.reserve_price = reserve_price
    db.session.commit()
    flash(f'{company.name} marked for exit with reserve price ${reserve_price:.1f}M.', 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


@app.route('/portfolio/company/<int:company_id>/followon', methods=['POST'])
@login_required
@block_if_ready
def follow_on(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if not deal or deal.lead_team_id != current_user.id:
        abort(403)
    if deal.status != 'active' or not company.in_distress:
        flash('Follow-on investment is only available when the company is in distress.', 'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))

    lead_stake = next((s for s in deal.equity_stakes
                       if s.team_id == current_user.id), None)
    fund = Fund.query.get(lead_stake.fund_id) if lead_stake else None
    if fund is None:
        abort(403)

    amount = float(request.form.get('amount', 0) or 0)
    if amount <= 0 or amount > fund.available_capital + 1e-6:
        flash(f'Follow-on amount must be between $0 and your fund\'s '
              f'${fund.available_capital:,.1f}M of available capital.', 'danger')
        return redirect(url_for('portfolio_company', company_id=company_id))

    process_followon(deal, amount)
    deal.let_it_roll = False   # they chose to invest, not roll
    db.session.commit()
    flash(f'Invested ${amount:,.1f}M into {company.name} at its current valuation. '
          f'Runway extended.', 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


@app.route('/portfolio/company/<int:company_id>/let-it-roll', methods=['POST'])
@login_required
@block_if_ready
def let_it_roll(company_id):
    """Lead explicitly chooses to forgo a rescue and let the turn-profitable
    roll decide at the next Deal & Return Process (non-mature only)."""
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal
    if not deal or deal.lead_team_id != current_user.id:
        abort(403)
    if deal.status != 'active' or not company.in_distress or company.stage == 'mature':
        flash('That choice is only available for a distressed non-mature company.', 'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))
    deal.let_it_roll = True
    db.session.commit()
    flash(f'You chose to let {company.name} roll — no rescue. It will take its '
          f'chance at turning profitable, and goes bankrupt if it does not.', 'info')
    return redirect(url_for('portfolio_company', company_id=company_id))


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------

@app.route('/funds')
@login_required
def funds():
    game = Game.query.get(current_user.game_id)
    team_funds = Fund.query.filter_by(team_id=current_user.id).all()
    # Chronological, oldest first — reads like a ledger (same as GP Economics)
    transactions = (FundTransaction.query
                    .join(Fund, FundTransaction.fund_id == Fund.id)
                    .filter(Fund.team_id == current_user.id)
                    .order_by(FundTransaction.game_year,
                              FundTransaction.created_at,
                              FundTransaction.id)
                    .all())
    # Carry the fund pays out to the GP, by fund name, so available capital can
    # be shown net of it (the fund distributes carry to the GP at exit)
    gp = team_gp_income(current_user)
    carry_by_fund = {cf['fund']: cf['carry'] for cf in gp['carry_funds']}
    ret = team_simple_return(current_user, game)
    return render_template('funds.html',
                           game=game,
                           team_funds=team_funds,
                           transactions=transactions,
                           carry_by_fund=carry_by_fund,
                           total_carry=gp['carried_interest'],
                           ret=ret)


@app.route('/gp-economics')
@login_required
def gp_economics():
    game = Game.query.get(current_user.game_id)
    gp = team_gp_income(current_user)
    return render_template('gp_economics.html', game=game, gp=gp)


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    game = current_game()
    teams = (Team.query.filter_by(game_id=game.id, is_admin=False).all()
             if game else [])
    companies = GameCompany.query.filter_by(game_id=game.id).all() if game else []
    # Deals scoped to this game via their company.
    deals = (Deal.query
             .join(GameCompany, Deal.company_id == GameCompany.id)
             .filter(GameCompany.game_id == game.id).all()
             if game else [])
    # All games for the switcher (newest first, archived ones hidden).
    all_games = (Game.query.filter_by(is_archived=False)
                 .order_by(Game.id.desc()).all())
    archived_games = (Game.query.filter_by(is_archived=True)
                      .order_by(Game.id.desc()).all())

    # Current-year term sheets grouped by company (sorted by company name)
    term_sheet_groups = []
    if game:
        sheets = (TermSheet.query
                  .join(GameCompany, TermSheet.company_id == GameCompany.id)
                  .filter(GameCompany.game_id == game.id,
                          TermSheet.game_year == game.current_year)
                  .order_by(GameCompany.name)
                  .all())
        by_company = {}
        for ts in sheets:
            by_company.setdefault(ts.company, []).append(ts)
        term_sheet_groups = sorted(by_company.items(), key=lambda x: x[0].name)

    ready_roster, ready_count, total_teams = (
        _readiness(game) if game else ([], 0, 0))
    return render_template('admin/dashboard.html',
                           game=game, teams=teams,
                           companies=companies, deals=deals,
                           term_sheet_groups=term_sheet_groups,
                           ready_roster=ready_roster,
                           ready_count=ready_count,
                           total_teams=total_teams,
                           all_games=all_games,
                           archived_games=archived_games)


def _seed_companies(game):
    """Insert a fresh set of companies into `game` from data/companies.json.
    Insert-only — assumes the game has no companies yet (e.g. a brand-new game).
    Returns the number of companies created."""
    from models import starting_burn_rate
    with open(os.path.join(_BASE_DIR, 'data', 'companies.json')) as f:
        company_data = json.load(f)
    for cd in company_data:
        db.session.add(GameCompany(
            game_id=game.id, name=cd['name'], sector=cd['sector'], stage=cd['stage'],
            description=cd['description'], capital_requested=cd['capital_requested'],
            rolled_equity_min=cd['rolled_equity_min'], rolled_equity_max=cd['rolled_equity_max'],
            debt_capacity=cd['debt_capacity'], is_cash_flow_positive=cd['is_cash_flow_positive'],
            dividend_eligible=cd.get('dividend_eligible', False),
            management_quality=cd['management_quality'],
            outcome_distributions=json.dumps(cd['outcome_distributions']),
            initial_val_ask=cd['base_valuation'], year_available=cd.get('year_available', 1),
            reasons_for_funding=cd.get('reasons_for_funding'),
            available_cash=(0.0 if cd['stage'] != 'mature' else cd.get('available_cash', 0.0)),
            founder_shares=cd.get('founder_shares', 10000000),
            management_option_pct=cd.get('management_option_pct', 0.10),
            revenue_growth_3yr=cd.get('revenue_growth_3yr'),
            ltm_ebitda_margin=cd.get('ltm_ebitda_margin'),
            ltm_revenue=cd.get('ltm_revenue'), ltm_ebitda=cd.get('ltm_ebitda'),
            annual_burn_rate=starting_burn_rate(cd['stage'], cd['capital_requested']),
        ))
    return len(company_data)


def _reload_companies_from_json(game):
    """Reset an EXISTING game's companies fresh from data/companies.json.

    Clears this game's per-company simulation state (deals, stakes, term sheets,
    searches, and company-linked transactions/notifications), deletes its
    companies, then re-seeds. Every clear is scoped to THIS game so resetting
    one game never touches another. Returns the number of companies loaded.
    """
    from sqlalchemy import text
    gid = {'g': game.id}
    comp = "SELECT id FROM game_company WHERE game_id = :g"
    db.session.execute(text(
        f"DELETE FROM deal_equity WHERE deal_id IN "
        f"(SELECT id FROM deal WHERE company_id IN ({comp}))"), gid)
    db.session.execute(text(f"DELETE FROM deal WHERE company_id IN ({comp})"), gid)
    db.session.execute(text(f"DELETE FROM company_search WHERE company_id IN ({comp})"), gid)
    db.session.execute(text(f"DELETE FROM term_sheet WHERE company_id IN ({comp})"), gid)
    db.session.execute(text(
        f"UPDATE fund_transaction SET company_id=NULL WHERE company_id IN ({comp})"), gid)
    db.session.execute(text(
        f"UPDATE notification SET related_company_id=NULL "
        f"WHERE related_company_id IN ({comp})"), gid)
    db.session.execute(text('DELETE FROM game_company WHERE game_id=:g'), gid)
    db.session.flush()
    return _seed_companies(game)


@app.route('/admin/reset-companies', methods=['POST'])
@login_required
@admin_required
def admin_reset_companies():
    game = current_game()
    n = _reload_companies_from_json(game)
    db.session.commit()
    flash(f'All {n} companies reloaded fresh from the source file — every '
          f'company is back to its authored state. (Any open deals and term '
          f'sheets were cleared.)', 'success')
    return redirect(url_for('admin_setup'))


@app.route('/admin/delete-all-deals', methods=['POST'])
@login_required
@admin_required
def admin_delete_all_deals():
    from sqlalchemy import text
    db.session.execute(text('DELETE FROM deal_equity'))
    db.session.execute(text('DELETE FROM deal'))
    db.session.execute(text(
        "UPDATE game_company SET status='available', year_funded=NULL, "
        "lead_team_id=NULL, in_distress=0, flagged_for_liquidation=0"
    ))
    db.session.commit()
    flash('All deals have been deleted and companies reset to available.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/setup', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_setup():
    if request.method == 'POST':
        from sqlalchemy import text
        db.session.expire_all()
        db.session.execute(text('DELETE FROM fund_transaction'))
        db.session.execute(text('DELETE FROM notification'))
        db.session.execute(text('DELETE FROM fund'))
        db.session.execute(text('DELETE FROM team WHERE is_admin = 0'))
        # Reload every company fresh so nothing carries over from the last game
        # (clears deals/stakes/term sheets/searches and resets all company state).
        _reload_companies_from_json(current_game())
        db.session.commit()
        db.session.expire_all()
        flash('All teams and their data have been removed, and every company '
              'was reloaded fresh.', 'success')
        return redirect(url_for('admin_setup'))

    return render_template('admin/setup.html')


@app.route('/admin/reset-clock', methods=['POST'])
@login_required
@admin_required
def admin_reset_clock():
    game = current_game()
    if game:
        game.current_year = 1
        game.current_phase = 1
        game.status = 'active'
        _clear_readiness(game)
        db.session.commit()
        flash('Game clock reset to Year 1, Phase 1.', 'success')
    else:
        flash('No game found.', 'warning')
    return redirect(url_for('admin_setup'))


@app.route('/admin/full-reset', methods=['POST'])
@login_required
@admin_required
def admin_full_reset():
    """One-click full reset: remove all teams, reload every company fresh, and
    reset the game clock to Year 1, Phase 1 — i.e. the other three setup
    actions combined into a single clean-slate operation."""
    from sqlalchemy import text
    db.session.expire_all()
    game = current_game()
    if not game:
        flash('No game found.', 'warning')
        return redirect(url_for('admin_setup'))
    # Remove all teams and their data.
    db.session.execute(text('DELETE FROM fund_transaction'))
    db.session.execute(text('DELETE FROM notification'))
    db.session.execute(text('DELETE FROM fund'))
    db.session.execute(text('DELETE FROM team WHERE is_admin = 0'))
    # Reload every company fresh (clears deals/stakes/term sheets/searches).
    n = _reload_companies_from_json(game)
    # Reset the game clock to the beginning.
    game.current_year = 1
    game.current_phase = 1
    game.status = 'active'
    db.session.commit()
    db.session.expire_all()
    flash(f'Full reset complete — all teams removed, {n} companies reloaded '
          f'fresh, and the game clock reset to Year 1, Phase 1.', 'success')
    return redirect(url_for('admin_setup'))


@app.route('/admin/teams')
@login_required
@admin_required
def admin_teams():
    game = current_game()
    teams = Team.query.filter_by(game_id=game.id, is_admin=False).all() if game else []
    return render_template('admin/teams.html', game=game, teams=teams,
                           sectors=SECTORS, fund_sizes=FUND_SIZE_PARTNERS)


@app.route('/admin/teams/create', methods=['POST'])
@login_required
@admin_required
def admin_create_team():
    game = current_game()
    if not game:
        flash('No game found. Please set up a game first.', 'warning')
        return redirect(url_for('admin_teams'))

    firm_name = request.form.get('firm_name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    fund_size = float(request.form.get('fund_size', 500))
    num_partners = FUND_SIZE_PARTNERS.get(int(fund_size), 5)
    sector_focus = request.form.get('sector_focus', 'generalist')
    fund_type = request.form.get('fund_type', 'pe')

    if not firm_name or not username or not password:
        flash('Firm name, username, and password are required.', 'danger')
        return redirect(url_for('admin_teams'))

    if Team.query.filter_by(username=username).first():
        flash(f'Username "{username}" is already taken.', 'danger')
        return redirect(url_for('admin_teams'))

    team = Team(
        game_id=game.id,
        username=username,
        firm_name=firm_name,
        reputation=5.0,
        sector_focus=sector_focus,
        fund_type=fund_type,
        num_partners=num_partners,
    )
    team.set_password(password)
    db.session.add(team)
    db.session.flush()

    management_fee, performance_fee = _parse_fee_structure()
    fund = Fund(
        team_id=team.id,
        name=f'{firm_name} Fund I',
        total_capital=fund_size,
        available_capital=fund_size,
        year_raised=game.current_year,
        management_fee_rate=management_fee,
        performance_fee_rate=performance_fee,
        operating_cost_rate=FUND_SIZE_OPEX.get(int(fund_size), 0.01),
    )
    db.session.add(fund)
    db.session.commit()
    flash(f'Team "{firm_name}" created successfully.', 'success')
    return redirect(url_for('admin_teams'))


@app.route('/admin/team/<int:team_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_team(team_id):
    team = Team.query.get_or_404(team_id)
    firm_name = team.firm_name

    # Remove the team's dealflow history so a recycled team id doesn't inherit it
    CompanySearch.query.filter_by(team_id=team.id).delete()
    CompanySearch.query.filter_by(referred_by_team_id=team.id).update(
        {'referred_by_team_id': None})

    # Unwind deals the team leads: refund co-investors, relist the company
    led_deals = Deal.query.filter_by(lead_team_id=team.id).all()
    for deal in led_deals:
        for stake in deal.equity_stakes:
            if stake.team_id != team.id and deal.status in ('pending_finalization', 'active'):
                fund = Fund.query.get(stake.fund_id)
                if fund:
                    fund.available_capital += stake.equity_invested
                    _record_transaction(fund.id, 'refund', stake.equity_invested,
                                        f"Refund: {deal.company.name} deal unwound "
                                        f"(lead team deleted)", deal.game_year,
                                        deal.company_id)
                _notify(stake.team_id,
                        f'The deal on {deal.company.name} was unwound because the '
                        f'lead investor was removed. Your investment was refunded.',
                        'deal_lost', deal.company_id)
        company = deal.company
        company.status = 'available'
        company.lead_team_id = None
        company.funded_valuation = None
        for y in range(1, GameCompany.MAX_TRACKED_YEARS + 1):
            company.set_year_val(y, None)
        company.year_funded = None
        company.debt_outstanding = 0.0
        company.debt_years_remaining = 0
        company.debt_interest_rate = 0.0
        company.company_funds = 0.0
        db.session.delete(deal)  # cascades to equity stakes

    # Fill stakes in deals led by other teams: just remove the stake
    DealEquity.query.filter_by(team_id=team.id).delete()

    # Companies still pointing at this team as lead (e.g. pre-finalization)
    GameCompany.query.filter_by(lead_team_id=team.id).update({'lead_team_id': None})

    db.session.delete(team)  # cascades: funds, term sheets, notifications
    db.session.commit()
    flash(f'Team "{firm_name}" has been deleted.', 'success')
    return redirect(url_for('admin_teams'))


@app.route('/admin/team/<int:team_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_team(team_id):
    team = Team.query.get_or_404(team_id)
    game = Game.query.get(team.game_id)
    if request.method == 'POST':
        team.firm_name = request.form.get('firm_name', team.firm_name)
        if request.form.get('fund_type') in ('pe', 'vc'):
            team.fund_type = request.form.get('fund_type')
        if 'sector_focus' in request.form:
            sf = request.form.get('sector_focus')
            if sf == 'generalist' or sf in SECTORS:
                team.sector_focus = sf
        new_pw = request.form.get('new_password', '').strip()
        if new_pw:
            team.set_password(new_pw)

        # Fund adjustments
        for fund in team.funds:
            key = f'fund_cap_{fund.id}'
            if key in request.form and request.form[key]:
                fund.available_capital = float(request.form[key])
            # Fee structure: only the two offered structures may be chosen;
            # 'keep' (or anything unrecognized) leaves the fund's rates as-is.
            fkey = f'fund_fees_{fund.id}'
            if request.form.get(fkey) in FEE_STRUCTURES:
                (fund.management_fee_rate,
                 fund.performance_fee_rate) = FEE_STRUCTURES[request.form[fkey]]

        db.session.commit()
        flash(f'{team.firm_name} updated.', 'success')
        return redirect(url_for('admin_teams'))
    return render_template('admin/edit_team.html', team=team, game=game,
                           sectors=SECTORS)


SECTORS = ['Consumer', 'Energy', 'Healthcare', 'Industrials', 'Technology']
STAGES = ['startup', 'developing', 'early_revenue', 'mature']
FUND_SIZE_PARTNERS = {200: 3, 500: 7, 1000: 12}          # fund size ($M) -> total partners
FUND_SIZE_OPEX = {200: 0.015, 500: 0.01, 1000: 0.0075}   # fund size ($M) -> GP operating cost %/yr
STAGE_LABELS = {'startup': 'Startup', 'developing': 'Developing',
                'early_revenue': 'Early Revenue', 'mature': 'Mature'}

# Authored baseline expected return / std dev by (sector, stage) for a
# sector-focused fund. This is the canonical default set seeded on a fresh DB
# (see _seed_return_assumptions). Admins can override on the Return Assumptions
# page; customized values are preserved across deploys.
RETURN_ASSUMPTION_DEFAULTS = {
    ('Consumer', 'startup'): (0.30, 0.85),
    ('Consumer', 'developing'): (0.22, 0.55),
    ('Consumer', 'early_revenue'): (0.16, 0.35),
    ('Consumer', 'mature'): (0.08, 0.18),
    ('Energy', 'startup'): (0.28, 0.75),
    ('Energy', 'developing'): (0.20, 0.50),
    ('Energy', 'early_revenue'): (0.14, 0.32),
    ('Energy', 'mature'): (0.095, 0.24),
    ('Healthcare', 'startup'): (0.40, 1.10),
    ('Healthcare', 'developing'): (0.28, 0.70),
    ('Healthcare', 'early_revenue'): (0.18, 0.40),
    ('Healthcare', 'mature'): (0.085, 0.20),
    ('Industrials', 'startup'): (0.26, 0.70),
    ('Industrials', 'developing'): (0.18, 0.45),
    ('Industrials', 'early_revenue'): (0.13, 0.28),
    ('Industrials', 'mature'): (0.09, 0.22),
    ('Technology', 'startup'): (0.35, 0.95),
    ('Technology', 'developing'): (0.25, 0.60),
    ('Technology', 'early_revenue'): (0.17, 0.38),
    ('Technology', 'mature'): (0.105, 0.26),
}


@app.route('/admin/return-assumptions', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_return_assumptions():
    if request.method == 'POST':
        for sector in SECTORS:
            for stage in STAGES:
                key = f'{sector}__{stage}'
                er = request.form.get(f'er_{key}')
                sd = request.form.get(f'sd_{key}')
                if er is not None and sd is not None:
                    ra = ReturnAssumption.query.filter_by(sector=sector, stage=stage).first()
                    if not ra:
                        ra = ReturnAssumption(sector=sector, stage=stage)
                        db.session.add(ra)
                    ra.expected_return = float(er) / 100
                    ra.std_dev = float(sd) / 100
        db.session.commit()
        flash('Return assumptions saved.', 'success')
        return redirect(url_for('admin_return_assumptions'))

    assumptions = {(ra.sector, ra.stage): ra for ra in ReturnAssumption.query.all()}
    return render_template('admin/return_assumptions.html',
                           sectors=SECTORS, stages=STAGES, stage_labels=STAGE_LABELS,
                           assumptions=assumptions)


@app.route('/admin/game-dynamics', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_game_dynamics():
    if request.method == 'POST':
        if 'reset' in request.form:
            game_settings.reset_all()
            flash('Game dynamics reset to defaults.', 'success')
            return redirect(url_for('admin_game_dynamics'))
        if 'reset_one' in request.form:
            label = game_settings.reset_one(request.form['reset_one'])
            if label:
                flash(f'"{label}" reset to its default.', 'success')
            return redirect(url_for('admin_game_dynamics'))
        ok, errors, changed = game_settings.save_from_form(request.form)
        if not ok:
            for e in errors:
                flash(e, 'danger')
        elif changed:
            flash('Game dynamics saved — they take effect on the next '
                  'Deal & Return Process and all future rolls.', 'success')
        else:
            flash('No changes to save.', 'info')
        return redirect(url_for('admin_game_dynamics'))

    return render_template('admin/game_dynamics.html',
                           groups=game_settings.GROUPS,
                           view=game_settings.current_view())


@app.route('/admin/companies')
@login_required
@admin_required
def admin_companies():
    game = current_game()
    f_status = request.args.get('status') or ''
    f_sector = request.args.get('sector') or ''
    f_stage = request.args.get('stage') or ''
    companies, total_count = [], 0
    if game:
        base = GameCompany.query.filter_by(game_id=game.id)
        total_count = base.count()
        q = base
        if f_status:
            q = q.filter_by(status=f_status)
        if f_sector:
            q = q.filter_by(sector=f_sector)
        if f_stage:
            q = q.filter_by(stage=f_stage)
        companies = q.order_by(GameCompany.year_available, GameCompany.name).all()

    # Capital the market is seeking, by sector x stage: funds wanted for
    # venture deals + asking (whole-company) valuation for buyouts.
    summary = {s: {st: 0.0 for st in STAGES} for s in SECTORS}
    sector_tot = {s: 0.0 for s in SECTORS}
    stage_tot = {st: 0.0 for st in STAGES}
    grand = 0.0
    for c in companies:
        val = (c.initial_val_ask or 0) if c.stage == 'mature' else (c.capital_requested or 0)
        if c.sector in summary and c.stage in summary[c.sector]:
            summary[c.sector][c.stage] += val
            sector_tot[c.sector] += val
            stage_tot[c.stage] += val
            grand += val

    return render_template('admin/companies.html', game=game, companies=companies,
                           total_count=total_count, sectors=SECTORS, stages=STAGES,
                           stage_labels=STAGE_LABELS, f_status=f_status,
                           f_sector=f_sector, f_stage=f_stage,
                           summary=summary, sector_tot=sector_tot,
                           stage_tot=stage_tot, grand=grand)


@app.route('/admin/company/<int:company_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_company(company_id):
    game = current_game()
    company = GameCompany.query.get_or_404(company_id)
    # Only edit companies in the game currently being managed.
    if not game or company.game_id != game.id:
        abort(404)
    if request.method == 'POST':
        company.name = request.form.get('name', company.name)
        company.description = request.form.get('description', company.description)
        iva = request.form.get('initial_val_ask')
        if iva:
            company.initial_val_ask = float(iva)
        cr = request.form.get('capital_requested')
        if cr:
            company.capital_requested = float(cr)
        fv = request.form.get('funded_valuation')
        company.funded_valuation = float(fv) if fv else None
        for y in range(1, GameCompany.MAX_TRACKED_YEARS + 1):
            yv = request.form.get(f'year_{y}_val')
            company.set_year_val(y, float(yv) if yv else None)
        company.status = request.form.get('status', company.status)
        company.management_quality = request.form.get('management_quality',
                                                       company.management_quality)

        if 'revenue_growth_3yr' in request.form:
            rg = request.form.get('revenue_growth_3yr')
            company.revenue_growth_3yr = float(rg) / 100 if rg else None
        if 'ltm_ebitda_margin' in request.form:
            em = request.form.get('ltm_ebitda_margin')
            company.ltm_ebitda_margin = float(em) / 100 if em else None
        if 'ltm_revenue' in request.form:
            lr = request.form.get('ltm_revenue')
            company.ltm_revenue = float(lr) if lr else None
        if 'ltm_ebitda' in request.form:
            le = request.form.get('ltm_ebitda')
            company.ltm_ebitda = float(le) if le else None

        # Invariant: a company with $0 revenue is burning cash, so its LTM EBITDA
        # must be negative (and its EBITDA margin is undefined — shown as N/A).
        if not company.ltm_revenue and (company.ltm_ebitda is None or company.ltm_ebitda >= 0):
            db.session.rollback()
            flash('A company with $0 revenue must have a negative LTM EBITDA — '
                  'no revenue means it is burning cash. (EBITDA margin is N/A.)',
                  'danger')
            return redirect(url_for('admin_edit_company', company_id=company_id))

        outcomes = []
        for i in range(12):
            multiple = request.form.get(f'multiple_{i}')
            prob = request.form.get(f'prob_{i}')
            if multiple is not None and prob is not None:
                outcomes.append({'multiple': float(multiple), 'prob': round(float(prob) / 100, 6)})
        if outcomes:
            import json as _json
            company.outcome_distributions = _json.dumps(outcomes)

        db.session.commit()
        flash(f'{company.name} updated.', 'success')
        return redirect(url_for('admin_companies'))
    return render_template('admin/edit_company.html', game=game, company=company)


@app.route('/admin/crank', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_crank():
    game = current_game()
    if not game:
        flash('No game found. Please set up a game first.', 'warning')
        return redirect(url_for('admin_setup'))

    if request.method == 'POST':
        # Support both 'crank_type' and 'crank_phase' field names
        crank_type = request.form.get('crank_type')
        crank_phase = request.form.get('crank_phase')
        if crank_phase == '1':
            crank_type = 'phase1'
        elif crank_phase == '2':
            crank_type = 'phase2'

        market_adj = float(request.form.get('market_condition') or game.market_condition)
        game.market_condition = market_adj

        if game.status == 'completed':
            flash('The game has ended — no further processes can be run.', 'warning')
        elif (crank_type == 'phase1' and game.current_phase == 1) or \
             (crank_type == 'phase2' and game.current_phase == 2):
            msg, cat = _run_current_phase_crank(game)
            _clear_readiness(game)
            db.session.commit()
            flash(msg, cat)
        else:
            flash('Invalid process for current phase.', 'danger')

        return redirect(url_for('admin_crank'))

    # Summary for admin
    pending_ts = (TermSheet.query
                  .join(GameCompany, TermSheet.company_id == GameCompany.id)
                  .filter(GameCompany.game_id == game.id,
                          TermSheet.game_year == game.current_year,
                          TermSheet.status == 'pending')
                  .count())
    pending_deals = (Deal.query
                     .join(GameCompany, Deal.company_id == GameCompany.id)
                     .filter(GameCompany.game_id == game.id,
                             Deal.game_year == game.current_year,
                             Deal.status == 'pending_finalization')
                     .count())

    return render_template('admin/crank.html',
                           game=game,
                           pending_ts=pending_ts,
                           pending_deals=pending_deals)


@app.route('/admin/game/pause', methods=['POST'])
@login_required
@admin_required
def admin_pause_game():
    game = current_game()
    game.status = 'paused' if game.status == 'active' else 'active'
    db.session.commit()
    flash(f'Game is now {game.status}.', 'info')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/auto-advance', methods=['POST'])
@login_required
@admin_required
def admin_toggle_auto_advance():
    game = current_game()
    if game:
        game.auto_advance = not bool(game.auto_advance)
        db.session.commit()
        state = 'ON' if game.auto_advance else 'OFF'
        # Turning it on when every team is already ready should fire immediately.
        fired = _maybe_auto_crank(game) if game.auto_advance else None
        if fired:
            msg, cat = fired
            flash(f'Auto-advance is now {state}. All teams were already '
                  f'marked complete — {msg}', cat)
        else:
            flash(f'Auto-advance is now {state}.', 'info')
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/game/create', methods=['POST'])
@login_required
@admin_required
def admin_create_game():
    """Create a new game, seed its companies, and switch the admin to it."""
    name = (request.form.get('name') or '').strip() or 'New Simulation'
    game = Game(name=name, current_year=1, current_phase=1, status='active',
                owner_id=current_user.id, join_code=_new_join_code())
    db.session.add(game)
    db.session.flush()   # assign game.id before seeding
    n = _seed_companies(game)
    db.session.commit()
    session['admin_game_id'] = game.id   # start managing the new game
    flash(f'Created "{game.name}" and seeded {n} companies — you are now '
          f'managing it. Add teams next, or share join code '
          f'{game.join_code} so teams can sign themselves up.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/game/select', methods=['POST'])
@login_required
@admin_required
def admin_select_game():
    """Switch which game the admin is currently managing."""
    game = Game.query.get(request.form.get('game_id', type=int))
    if game:
        session['admin_game_id'] = game.id
        flash(f'Now managing "{game.name}".', 'info')
    else:
        flash('Game not found.', 'warning')
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/game/archive', methods=['POST'])
@login_required
@admin_required
def admin_archive_game():
    """Archive a game — hides it from the switcher but keeps all its data."""
    game = Game.query.get(request.form.get('game_id', type=int))
    if not game:
        flash('Game not found.', 'warning')
        return redirect(url_for('admin_dashboard'))
    game.is_archived = True
    db.session.commit()
    if session.get('admin_game_id') == game.id:
        session.pop('admin_game_id', None)   # fall back to another active game
    flash(f'Archived "{game.name}" — its team logins are disabled and anyone '
          f'signed in is logged out. All data is kept; restore the game to '
          f're-enable it.', 'info')
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/game/unarchive', methods=['POST'])
@login_required
@admin_required
def admin_unarchive_game():
    """Bring an archived game back into the active switcher and manage it."""
    game = Game.query.get(request.form.get('game_id', type=int))
    if not game:
        flash('Game not found.', 'warning')
        return redirect(url_for('admin_dashboard'))
    game.is_archived = False
    db.session.commit()
    session['admin_game_id'] = game.id   # switch to the restored game
    flash(f'Restored "{game.name}" — you are now managing it.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/games')
@login_required
@admin_required
def admin_games_overview():
    """Cross-game overview: every game's phase/status and counts at a glance."""
    current = current_game()
    games = Game.query.order_by(Game.is_archived.asc(), Game.id.desc()).all()
    rows = []
    for g in games:
        rows.append({
            'game': g,
            'teams': Team.query.filter_by(game_id=g.id, is_admin=False).count(),
            'companies': GameCompany.query.filter_by(game_id=g.id).count(),
            'active_deals': (Deal.query
                             .join(GameCompany, Deal.company_id == GameCompany.id)
                             .filter(GameCompany.game_id == g.id,
                                     Deal.status == 'active').count()),
        })
    return render_template('admin/games.html', rows=rows,
                           current_id=(current.id if current else None))


@app.route('/admin/all-teams')
@login_required
@admin_required
def admin_all_teams():
    """Master roster: every team across every game, with login credentials
    (passwords are stored in the clear for classroom handout) and mandate."""
    rows = (db.session.query(Team, Game)
            .join(Game, Team.game_id == Game.id)
            .filter(Team.is_admin.is_(False))
            .order_by(Game.is_archived.asc(), Game.id.desc(), Team.username)
            .all())
    return render_template('admin/all_teams.html', rows=rows)


@app.route('/admin/game/market', methods=['POST'])
@login_required
@admin_required
def admin_set_market():
    game = current_game()
    game.market_condition = float(request.form.get('market_condition', 1.0))
    db.session.commit()
    flash(f'Market condition set to {game.market_condition:.2f}x.', 'success')
    return redirect(url_for('admin_dashboard'))


# Sortable leaderboard columns -> key into each team's row dict.
LEADERBOARD_SORTS = {
    'committed': lambda x: x['ret']['annualized'],   # Return on Committed Capital (IRR)
    'invested': lambda x: x['ret']['deal_irr'],      # Return on Invested Capital (deal IRR)
    'gp': lambda x: x['gp_income']['per_partner'],   # GP Income / Partner
}


def _leaderboard_team_data(game, sort='committed'):
    """Build the ranked per-team rows for the leaderboard. Shared by the admin
    view and the post-game team view. `sort` selects the ranking column."""
    teams = Team.query.filter_by(game_id=game.id, is_admin=False).all()
    team_data = []
    for team in teams:
        total_deployed = sum(
            s.equity_invested for s in DealEquity.query.filter_by(team_id=team.id).all())
        stakes = (DealEquity.query
                  .join(Deal, DealEquity.deal_id == Deal.id)
                  .filter(DealEquity.team_id == team.id, Deal.status == 'active')
                  .all())
        portfolio_val = sum(s.current_value for s in stakes)
        closed_count = (DealEquity.query
                        .join(Deal, DealEquity.deal_id == Deal.id)
                        .filter(DealEquity.team_id == team.id,
                                Deal.status.in_(['liquidated', 'bankrupt']))
                        .count())

        ret = team_simple_return(team, game)
        gp_income = team_gp_income(team)
        team_data.append({
            'team': team,
            'ret': ret,
            'total_capital': sum(f.total_capital for f in team.funds),
            'available_capital': team.total_available_capital,
            'deployed': total_deployed,
            'portfolio_value': portfolio_val,
            'reputation': team.reputation,
            'active_deals': len(stakes),
            'closed_deals': closed_count,
            'gp_income': gp_income,
        })

    keyfn = LEADERBOARD_SORTS.get(sort, LEADERBOARD_SORTS['committed'])
    team_data.sort(key=keyfn, reverse=True)
    return team_data


@app.route('/admin/leaderboard')
@login_required
@admin_required
def admin_leaderboard():
    game = current_game()
    sort = request.args.get('sort', 'invested')
    if sort not in LEADERBOARD_SORTS:
        sort = 'invested'
    return render_template('admin/leaderboard.html', game=game, sort=sort,
                           team_data=_leaderboard_team_data(game, sort))


@app.route('/leaderboard')
@login_required
def leaderboard():
    """Team-facing leaderboard — available to everyone once the game is over."""
    if current_user.is_admin:
        return redirect(url_for('admin_leaderboard'))
    game = Game.query.get(current_user.game_id)
    if not game or not game.is_complete:
        flash('The leaderboard opens once the game is complete.', 'info')
        return redirect(url_for('dashboard'))
    sort = request.args.get('sort', 'invested')
    if sort not in LEADERBOARD_SORTS:
        sort = 'invested'
    return render_template('admin/leaderboard.html', game=game, sort=sort,
                           team_data=_leaderboard_team_data(game, sort))


# ---------------------------------------------------------------------------
# API endpoints (for AJAX refreshes)
# ---------------------------------------------------------------------------

@app.route('/api/game/status')
@login_required
def api_game_status():
    game = Game.query.get(current_user.game_id)
    roster, ready_count, total_teams = _readiness(game)
    return jsonify({
        'year': game.current_year,
        'phase': game.current_phase,
        'status': game.status,
        'label': game.phase_label,
        'phase_label': game.phase_label,
        'is_paused': game.status == 'paused',
        'unread': current_user.unread_notifications,
        'auto_advance': bool(game.auto_advance),
        'ready_count': ready_count,
        'total_teams': total_teams,
        'roster': roster,
        'i_am_ready': (False if current_user.is_admin
                       else current_user.is_ready_for(game)),
    })


# ---------------------------------------------------------------------------
# DB Init
# ---------------------------------------------------------------------------

def _ensure_schema():
    """Additive migration for columns introduced after an existing DB was
    created. db.create_all() only creates missing *tables*, not missing
    *columns*, so new columns on existing tables are added here. Idempotent,
    and dialect-aware for the one boolean default (SQLite vs Postgres)."""
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    bool_default = 'DEFAULT 1' if db.engine.dialect.name == 'sqlite' else 'DEFAULT TRUE'
    bool_false = 'DEFAULT 0' if db.engine.dialect.name == 'sqlite' else 'DEFAULT FALSE'
    wanted = {
        'game': [('auto_advance', f'BOOLEAN {bool_default}'),
                 ('owner_id', 'INTEGER'),
                 ('is_archived', f'BOOLEAN {bool_false}'),
                 ('join_code', 'VARCHAR(12)')],
        'team': [('ready_year', 'INTEGER'), ('ready_phase', 'INTEGER'),
                 ('last_login', 'TIMESTAMP'), ('last_seen', 'TIMESTAMP')],
    }
    for table, cols in wanted.items():
        if not insp.has_table(table):
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, coltype in cols:
            if name not in existing:
                db.session.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {name} {coltype}'))
    db.session.commit()


def _seed_return_assumptions():
    """Ensure the authored baseline ReturnAssumption (RETURN_ASSUMPTION_DEFAULTS)
    exists for every sector/stage combo. Creates any missing row, and corrects a
    row that still holds the old generic 10%/20% placeholder to its authored
    value (a one-time fix for DBs seeded before the authored defaults existed).
    Admin-customized values — anything other than that placeholder — are left
    untouched. Without this, a fresh DB shows a blank Valuation Forecast and the
    crank has no sector/stage baseline."""
    PLACEHOLDER = (0.10, 0.20)  # the earlier model-default seed, safe to replace
    existing = {(ra.sector, ra.stage): ra for ra in ReturnAssumption.query.all()}
    changed = 0
    for (sector, stage), (er, sd) in RETURN_ASSUMPTION_DEFAULTS.items():
        ra = existing.get((sector, stage))
        if ra is None:
            db.session.add(ReturnAssumption(sector=sector, stage=stage,
                                            expected_return=er, std_dev=sd))
            changed += 1
        elif (round(ra.expected_return, 6), round(ra.std_dev, 6)) == PLACEHOLDER:
            ra.expected_return, ra.std_dev = er, sd
            changed += 1
    if changed:
        db.session.commit()
        print(f"[init_db] Seeded/corrected {changed} return assumptions.")


def init_db():
    with app.app_context():
        # Report the active DB backend so deploy logs make it obvious whether
        # production is on persistent Postgres or (accidentally) ephemeral
        # SQLite, which resets every deploy.
        backend = db.engine.dialect.name
        print(f"[init_db] Database backend: {backend}")
        if backend == 'sqlite' and not os.environ.get('DATABASE_URL'):
            print("[init_db] WARNING: no DATABASE_URL set - using local SQLite. "
                  "On an ephemeral host this wipes all data on every deploy.")
        db.create_all()
        _ensure_schema()
        _seed_return_assumptions()
        # Load any saved Game Dynamics overrides onto the live module globals.
        game_settings.apply_overrides()
        # Create admin user if none exists
        admin = Team.query.filter_by(username='admin').first()
        if not admin:
            # Need a placeholder game_id; create a default game. Runs outside any
            # request (no current_user/session), so query the game directly.
            game = Game.query.first()
            if not game:
                game = Game(name='SimulateGP', current_year=1, current_phase=1)
                db.session.add(game)
                db.session.flush()
            admin = Team(
                game_id=game.id,
                username='admin',
                firm_name='Administrator',
                is_admin=True,
                reputation=5.0
            )
            admin.set_password(os.environ.get('ADMIN_PASSWORD', 'admin123'))
            db.session.add(admin)
            db.session.commit()
            print("Admin user created: username=admin")
        # Backfill join codes for any game without one (legacy games and the
        # bootstrap game created just above) — must run AFTER game creation.
        missing = Game.query.filter(Game.join_code.is_(None)).all()
        if missing:
            for g in missing:
                g.join_code = _new_join_code()
            db.session.commit()
            print(f"[init_db] Backfilled join codes for {len(missing)} game(s).")


if __name__ == '__main__':
    # Local dev only — gunicorn imports `app:app` and never runs this block,
    # so the auto-reloader stays out of the production path. The reloader
    # restarts the server on any .py change so edits take effect without a
    # manual restart (templates already refresh per request).
    init_db()
    app.run(debug=True, port=5000, use_reloader=True)
