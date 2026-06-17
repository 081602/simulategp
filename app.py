import os
import json
import random
from datetime import datetime
from sqlalchemy import or_, and_
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, abort, session)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from models import (db, Game, Team, Fund, CompanyTemplate, GameCompany,
                    CompanySearch, TermSheet, Deal, DealEquity,
                    FundTransaction, Notification, ReturnAssumption)
from game_logic import (run_phase1_crank, run_phase2_crank,
                        team_simple_return, team_gp_income,
                        finalize_deal, close_deal_with_coinvestors,
                        locked_deal_economics, exit_waterfall,
                        DEBT_INTEREST_RATE, _notify, _record_transaction)

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


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        team = Team.query.filter_by(username=username).first()
        if team and team.check_password(password):
            login_user(team, remember=True)
            return redirect(url_for('index'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


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
    ret = team_simple_return(current_user, game)
    return render_template('dashboard.html',
                           game=game,
                           notifications=notifications,
                           active_deals=active_deals,
                           bid_activity=bid_activity,
                           ret=ret)


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
def search_companies():
    game = Game.query.get(current_user.game_id)
    if game.current_phase != 1:
        flash('Company search is only available in Phase 1.', 'warning')
        return redirect(url_for('dealflow'))

    all_sectors = db.session.query(GameCompany.sector).filter_by(
        game_id=game.id).distinct().all()
    sectors = sorted(set(s[0].split('/')[0].strip() for s in all_sectors))

    results = None  # None = no search submitted yet; searches are free/unlimited
    if request.method == 'POST':
        results = []
        sector_filter = request.form.get('sector', '')
        stage_filter = request.form.get('stage', '')
        min_deal_size = request.form.get('min_deal_size', '')
        max_deal_size = request.form.get('max_deal_size', '')

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

        if not results:
            flash('No new companies found matching your criteria. Try relaxing your search parameters.', 'info')

    return render_template('dealflow/search.html',
                           game=game,
                           results=results,
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


@app.route('/company/<int:company_id>/termsheet', methods=['GET', 'POST'])
@login_required
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
            total_investment = float(request.form['total_investment'])
            # Single rolled-equity number offered (stored in both legacy columns)
            rolled_val = float(request.form.get('rolled_equity')
                               or request.form.get('rolled_equity_min')) / 100
            rolled_min = rolled_max = rolled_val
            fund_id = int(request.form['fund_id'])
            liq_pref = int(request.form.get('liquidation_preference', 1))
            participation = 'participation' in request.form
            anti_dilution = request.form.get('anti_dilution', 'none')
            willing_fill = 'willing_to_fill' in request.form
            max_fill = float(request.form.get('max_fill_equity') or 0)
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

            # Validate rolled equity within company's range
            if not (company.rolled_equity_min <= rolled_min <= company.rolled_equity_max and
                    company.rolled_equity_min <= rolled_max <= company.rolled_equity_max):
                flash(f'Rolled equity must be between '
                      f'{company.rolled_equity_min*100:.0f}% and '
                      f'{company.rolled_equity_max*100:.0f}%.', 'danger')
                return redirect(request.url)

            # Buyouts must be financeable: the debt the structure implies
            # cannot exceed the company's capacity (terms are binding)
            if company.stage == 'mature' and rolled_val < 1:
                implied_debt = pre_money - total_investment / (1 - rolled_val)
                if implied_debt > company.debt_capacity + 1e-6:
                    flash(f'This structure implies ${implied_debt:,.1f}M of debt — '
                          f'above {company.name}\'s ${company.debt_capacity:,.1f}M '
                          f'capacity. Raise your equity check or lower the price.',
                          'danger')
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

@app.route('/timeline')
@login_required
def timeline():
    game = Game.query.get(current_user.game_id)

    # My term sheets this year
    my_ts = (TermSheet.query
             .filter_by(team_id=current_user.id, game_year=game.current_year)
             .all())

    # Deals where I'm lead and need to finalize
    pending_deals = (Deal.query
                     .filter_by(lead_team_id=current_user.id,
                                game_year=game.current_year,
                                status='pending_finalization')
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

    return render_template('timeline.html',
                           game=game,
                           my_term_sheets=my_ts,
                           pending_deals=pending_deals,
                           coinvest_offers=coinvest_offers,
                           awaiting_coinvest=awaiting_coinvest,
                           my_funds=my_funds)


@app.route('/timeline/coinvest/<int:ts_id>', methods=['POST'])
@login_required
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

            # Build the lead's co-investment proposals (capped at each fill's max)
            proposals = []
            proposed_total = 0.0
            selected_fills = request.form.getlist('selected_fills')
            for fill_ts_id in selected_fills:
                fts = TermSheet.query.get(int(fill_ts_id))
                if not fts or fts.status != 'fill_offered':
                    continue
                amount = min(float(request.form.get(f'fill_equity_{fill_ts_id}')
                                   or fts.max_fill_equity),
                             fts.max_fill_equity)
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
                return redirect(url_for('portfolio'))

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
            val_history.append({'year': y, 'value': v,
                                'ret': (v / prev - 1) if prev else None})
            prev = v

    return render_template('portfolio/company.html',
                           game=game,
                           company=company,
                           deal=deal,
                           stake=stake,
                           waterfall=waterfall,
                           return_assumption=return_assumption,
                           val_history=val_history,
                           is_lead=is_lead,
                           funds=funds,
                           all_teams=all_teams)


@app.route('/portfolio/company/<int:company_id>/change_mgmt', methods=['POST'])
@login_required
def change_management(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)
    if company.year_funded >= game.current_year:
        flash('Management can only be changed after at least 1 year in portfolio.', 'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))

    cost = (company.latest_valuation or 10.0) * 0.10
    primary_fund = Fund.query.filter_by(
        team_id=current_user.id, is_active=True).first()

    if not primary_fund or primary_fund.available_capital < cost:
        flash(f'Insufficient funds. Management change costs ${cost:.2f}M.', 'danger')
        return redirect(url_for('portfolio_company', company_id=company_id))

    primary_fund.available_capital -= cost
    _record_transaction(primary_fund.id, 'management_change_fee', -cost,
                        f'Management change at {company.name}', game.current_year, company.id)

    # Randomly assign new management quality
    qualities = ['weak', 'average', 'average', 'strong']
    company.management_quality = random.choice(qualities)
    # Slight reputation hit for instability
    current_user.reputation = max(1.0, current_user.reputation - 0.1)
    db.session.commit()
    flash(f'Management team replaced at {company.name}. Cost: ${cost:.2f}M. '
          f'New management quality: {company.management_quality}.', 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


@app.route('/portfolio/company/<int:company_id>/dividend', methods=['POST'])
@login_required
def issue_dividend(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)
    if not company.is_cash_flow_positive:
        flash('This company is not eligible for dividends.', 'warning')
        return redirect(url_for('portfolio_company', company_id=company_id))

    # Max dividend = 20% of company funds
    max_div = company.company_funds * 0.20
    amount = float(request.form.get('amount', 0))
    if amount <= 0 or amount > max_div:
        flash(f'Dividend amount must be between $0 and ${max_div:.2f}M.', 'danger')
        return redirect(url_for('portfolio_company', company_id=company_id))

    company.company_funds -= amount

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
def mark_liquidation(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    deal = company.deal

    if deal.lead_team_id != current_user.id:
        abort(403)

    reserve_price = float(request.form.get('reserve_price', 0))
    deal.marked_for_liquidation = True
    deal.reserve_price = reserve_price
    db.session.commit()
    flash(f'{company.name} marked for exit with reserve price ${reserve_price:.1f}M.', 'success')
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
    game = Game.query.first()
    teams = Team.query.filter_by(is_admin=False).all() if game else []
    companies = GameCompany.query.filter_by(game_id=game.id).all() if game else []
    deals = Deal.query.all() if game else []

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

    return render_template('admin/dashboard.html',
                           game=game, teams=teams,
                           companies=companies, deals=deals,
                           term_sheet_groups=term_sheet_groups)


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
        db.session.execute(text('DELETE FROM deal_equity'))
        db.session.execute(text('DELETE FROM deal'))
        db.session.execute(text('DELETE FROM company_search'))
        db.session.execute(text('DELETE FROM term_sheet'))
        db.session.execute(text('DELETE FROM fund_transaction'))
        db.session.execute(text('DELETE FROM notification'))
        db.session.execute(text('DELETE FROM fund'))
        db.session.execute(text('DELETE FROM team WHERE is_admin = 0'))
        db.session.execute(text(
            "UPDATE game_company SET status='available', year_funded=NULL, "
            "lead_team_id=NULL, in_distress=0, flagged_for_liquidation=0, "
            "funded_valuation=NULL, year_1_val=NULL, year_2_val=NULL, "
            "year_3_val=NULL, year_4_val=NULL, year_5_val=NULL, "
            "year_6_val=NULL, year_7_val=NULL"
        ))
        db.session.commit()
        db.session.expire_all()
        flash('All teams and their data have been removed.', 'success')
        return redirect(url_for('admin_teams'))

    return render_template('admin/setup.html')


@app.route('/admin/reset-clock', methods=['POST'])
@login_required
@admin_required
def admin_reset_clock():
    game = Game.query.first()
    if game:
        game.current_year = 1
        game.current_phase = 1
        game.status = 'active'
        db.session.commit()
        flash('Game clock reset to Year 1, Phase 1.', 'success')
    else:
        flash('No game found.', 'warning')
    return redirect(url_for('admin_setup'))


@app.route('/admin/teams')
@login_required
@admin_required
def admin_teams():
    game = Game.query.first()
    teams = Team.query.filter_by(game_id=game.id, is_admin=False).all() if game else []
    return render_template('admin/teams.html', game=game, teams=teams,
                           sectors=SECTORS, fund_sizes=FUND_SIZE_PARTNERS)


@app.route('/admin/teams/create', methods=['POST'])
@login_required
@admin_required
def admin_create_team():
    game = Game.query.first()
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

    management_fee = float(request.form.get('management_fee', 2.0)) / 100
    performance_fee = float(request.form.get('performance_fee', 20.0)) / 100
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
            mkey = f'fund_mgmt_{fund.id}'
            if mkey in request.form and request.form[mkey]:
                fund.management_fee_rate = float(request.form[mkey]) / 100
            pkey = f'fund_perf_{fund.id}'
            if pkey in request.form and request.form[pkey]:
                fund.performance_fee_rate = float(request.form[pkey]) / 100

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


@app.route('/admin/companies')
@login_required
@admin_required
def admin_companies():
    game = Game.query.first()
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
    game = Game.query.first()
    company = GameCompany.query.get_or_404(company_id)
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
        company.is_cash_flow_positive = 'is_cash_flow_positive' in request.form

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
    game = Game.query.first()
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
        elif crank_type == 'phase1' and game.current_phase == 1:
            game.status = 'in_crank'
            db.session.commit()
            run_phase1_crank(game)
            flash(f'Deal Process complete. Year {game.current_year} Phase 2 is now open.', 'success')
        elif crank_type == 'phase2' and game.current_phase == 2:
            game.status = 'in_crank'
            db.session.commit()
            run_phase2_crank(game)
            if game.status == 'completed':
                flash(f'The fund\'s term has ended after Year {game.current_year}. '
                      f'All remaining holdings were exited — the game is complete. '
                      f'See the leaderboard for final results.', 'success')
            else:
                flash(f'Deal & Return Process complete. Year {game.current_year} Phase 1 is now open.', 'success')
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
    pending_deals = Deal.query.filter_by(
        game_year=game.current_year, status='pending_finalization').count()

    return render_template('admin/crank.html',
                           game=game,
                           pending_ts=pending_ts,
                           pending_deals=pending_deals)


@app.route('/admin/game/pause', methods=['POST'])
@login_required
@admin_required
def admin_pause_game():
    game = Game.query.first()
    game.status = 'paused' if game.status == 'active' else 'active'
    db.session.commit()
    flash(f'Game is now {game.status}.', 'info')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/game/market', methods=['POST'])
@login_required
@admin_required
def admin_set_market():
    game = Game.query.first()
    game.market_condition = float(request.form.get('market_condition', 1.0))
    db.session.commit()
    flash(f'Market condition set to {game.market_condition:.2f}x.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/leaderboard')
@login_required
@admin_required
def admin_leaderboard():
    game = Game.query.first()
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
            'deal_count': len(stakes),
            'gp_income': gp_income,
        })

    team_data.sort(key=lambda x: x['ret']['annualized'], reverse=True)
    return render_template('admin/leaderboard.html', game=game, team_data=team_data)


# ---------------------------------------------------------------------------
# API endpoints (for AJAX refreshes)
# ---------------------------------------------------------------------------

@app.route('/api/game/status')
@login_required
def api_game_status():
    game = Game.query.get(current_user.game_id)
    return jsonify({
        'year': game.current_year,
        'phase': game.current_phase,
        'status': game.status,
        'label': game.phase_label,
        'unread': current_user.unread_notifications
    })


# ---------------------------------------------------------------------------
# DB Init
# ---------------------------------------------------------------------------

def init_db():
    with app.app_context():
        db.create_all()
        # Create admin user if none exists
        admin = Team.query.filter_by(username='admin').first()
        if not admin:
            # Need a placeholder game_id; create a default game
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


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000, use_reloader=False)
