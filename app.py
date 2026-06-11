import os
import json
import random
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, abort)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from models import (db, Game, Team, Fund, CompanyTemplate, GameCompany,
                    CompanySearch, TermSheet, Deal, DealEquity,
                    FundTransaction, Notification, ReturnAssumption)
from game_logic import (run_phase1_crank, run_phase2_crank,
                        team_irr, team_gp_income, finalize_deal,
                        _notify, _record_transaction)

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
    realized_irr = team_irr(current_user.id, game, unrealized=False)
    unrealized_irr = team_irr(current_user.id, game, unrealized=True)
    return render_template('dashboard.html',
                           game=game,
                           notifications=notifications,
                           active_deals=active_deals,
                           realized_irr=realized_irr,
                           unrealized_irr=unrealized_irr)


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
    # Companies this team has already found this year
    found_searches = (CompanySearch.query
                      .filter_by(team_id=current_user.id,
                                 game_year=game.current_year)
                      .all())
    found_company_ids = [s.company_id for s in found_searches]
    found_companies = GameCompany.query.filter(
        GameCompany.id.in_(found_company_ids)).all() if found_company_ids else []

    # Categorize: searched vs referred
    search_map = {s.company_id: s for s in found_searches}
    return render_template('dealflow/index.html',
                           game=game,
                           found_companies=found_companies,
                           search_map=search_map)


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
        min_funds = request.form.get('min_funds', '')
        max_funds = request.form.get('max_funds', '')

        # Build query
        query = GameCompany.query.filter_by(
            game_id=game.id, status='available').filter(
            GameCompany.year_available <= game.current_year)

        if sector_filter:
            query = query.filter(GameCompany.sector == sector_filter)
        if stage_filter:
            query = query.filter(GameCompany.stage == stage_filter)
        if min_funds:
            try:
                query = query.filter(
                    GameCompany.capital_requested >= float(min_funds))
            except ValueError:
                pass
        if max_funds:
            try:
                query = query.filter(
                    GameCompany.capital_requested <= float(max_funds))
            except ValueError:
                pass

        companies = query.all()

        # Already found companies
        already_found = set(
            s.company_id for s in CompanySearch.query.filter_by(
                team_id=current_user.id, game_year=game.current_year).all())

        new_matches = [c for c in companies if c.id not in already_found]
        if len(new_matches) > MAX_SEARCH_RESULTS:
            new_matches = random.sample(new_matches, MAX_SEARCH_RESULTS)
            flash(f'Your search matched more companies than your analysts '
                  f'could evaluate — showing {MAX_SEARCH_RESULTS}. '
                  f'Narrow your criteria to see specific targets.', 'info')

        for c in new_matches:
            cs = CompanySearch(
                team_id=current_user.id,
                company_id=c.id,
                game_year=game.current_year,
                found_by_search=True
            )
            db.session.add(cs)
            results.append(c)

        # Shamrock: small chance of finding one extra company outside criteria
        all_available = GameCompany.query.filter_by(
            game_id=game.id, status='available').filter(
            GameCompany.year_available <= game.current_year).all()
        unseen = [c for c in all_available
                  if c.id not in already_found and c not in results]
        if unseen and random.random() < 0.3:
            bonus = random.choice(unseen)
            cs = CompanySearch(
                team_id=current_user.id,
                company_id=bonus.id,
                game_year=game.current_year,
                found_by_search=True
            )
            db.session.add(cs)
            results.append(bonus)
            flash(f'Your analysts stumbled upon an additional opportunity: {bonus.name}!', 'info')

        db.session.commit()
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

    # Confirm team has found this company
    search_record = CompanySearch.query.filter_by(
        team_id=current_user.id, company_id=company_id,
        game_year=game.current_year).first()
    # Admin can always view
    if not search_record and not current_user.is_admin:
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
                           return_assumption=return_assumption)


@app.route('/company/<int:company_id>/refer', methods=['POST'])
@login_required
def refer_company(company_id):
    game = Game.query.get(current_user.game_id)
    company = GameCompany.query.filter_by(id=company_id, game_id=game.id).first_or_404()
    target_team_id = request.form.get('target_team_id', type=int)
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

    block_reason = current_user.investment_block_reason(company)
    if block_reason:
        flash(block_reason, 'danger')
        return redirect(url_for('company_detail', company_id=company_id))

    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all()
    # Only offer syndicate partners whose mandate also allows this company
    teams = [t for t in Team.query.filter_by(game_id=game.id, is_admin=False)
             .filter(Team.id != current_user.id).all()
             if t.investment_block_reason(company) is None]

    if request.method == 'POST':
        try:
            pre_money = float(request.form['pre_money_valuation'])
            total_investment = float(request.form['total_investment'])
            rolled_min = float(request.form['rolled_equity_min']) / 100
            rolled_max = float(request.form['rolled_equity_max']) / 100
            fund_id = int(request.form['fund_id'])
            liq_pref = int(request.form.get('liquidation_preference', 1))
            participation = 'participation' in request.form
            anti_dilution = request.form.get('anti_dilution', 'none')
            willing_fill = 'willing_to_fill' in request.form
            max_fill = float(request.form.get('max_fill_equity') or 0)
            min_rep = float(request.form.get('min_lead_reputation') or 0)
            ts_type = request.form.get('term_sheet_type') or request.form.get('ts_type', 'solo')
            syndicate_ids = request.form.getlist('syndicate_partners')
            syndicate_ids = [int(x) for x in syndicate_ids if x]

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
                term_sheet_type=ts_type,
                syndicate_partners=json.dumps(syndicate_ids),
                syndicate_approved_by=json.dumps([current_user.id])
            )
            db.session.add(ts)
            db.session.flush()

            # Notify syndicate partners
            for pid in syndicate_ids:
                _notify(pid,
                        f'{current_user.firm_name} has invited you to join a syndicate '
                        f'term sheet on {company.name}. Please review and approve on the Timeline.',
                        'fill_offered', company_id)

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

    # Syndicate term sheets awaiting my approval
    all_ts = (TermSheet.query
              .filter_by(game_year=game.current_year,
                         term_sheet_type='syndicate')
              .filter(TermSheet.status.in_(['pending']))
              .all())

    pending_syndicate_approval = []
    for ts in all_ts:
        partners = ts.get_syndicate_partners()
        approved_by = ts.get_syndicate_approved_by()
        if current_user.id in partners and current_user.id not in approved_by:
            pending_syndicate_approval.append(ts)

    return render_template('timeline.html',
                           game=game,
                           my_term_sheets=my_ts,
                           pending_deals=pending_deals,
                           pending_syndicate_approval=pending_syndicate_approval)


@app.route('/timeline/syndicate/approve/<int:ts_id>', methods=['POST'])
@login_required
def approve_syndicate(ts_id):
    ts = TermSheet.query.get_or_404(ts_id)
    decision = request.form.get('decision', 'approve')
    if decision == 'decline':
        # Remove current user from syndicate partners list
        partners = ts.get_syndicate_partners()
        if current_user.id in partners:
            partners.remove(current_user.id)
            ts.syndicate_partners = json.dumps(partners)
            db.session.commit()
        flash('You have declined the syndicate invitation.', 'info')
    else:
        approved_by = ts.get_syndicate_approved_by()
        if current_user.id not in approved_by:
            approved_by.append(current_user.id)
            ts.syndicate_approved_by = json.dumps(approved_by)
            db.session.commit()
            flash('Syndicate term sheet approved.', 'success')
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
        action = request.form.get('action')
        if action == 'drop':
            deal.status = 'dropped'
            company.status = 'available'
            company.lead_team_id = None
            reason = request.form.get('drop_reason', '')
            # Reputation hit
            current_user.reputation = max(1.0, current_user.reputation - 0.5)
            _notify(current_user.id,
                    f'You dropped the deal on {company.name}.',
                    'deal_lost', company.id)
            db.session.commit()
            flash(f'Deal on {company.name} dropped.', 'warning')
            return redirect(url_for('timeline'))

        try:
            final_pre_money = float(request.form.get('pre_money_valuation') or request.form.get('final_pre_money', lead_ts.pre_money_valuation))
            # Validate: can't go below 90% of original bid
            min_allowed = lead_ts.pre_money_valuation * 0.90
            if final_pre_money < min_allowed:
                flash(f'Final valuation cannot be less than ${min_allowed:.1f}M '
                      f'(90% of your original bid).', 'danger')
                return redirect(request.url)

            my_equity = float(request.form.get('my_equity') or request.form.get('my_equity_contribution', 0))
            rolled_pct = float(request.form.get('rolled_equity_pct') or 90) / 100
            debt_amount = float(request.form.get('debt_amount') or 0)
            debt_rate = float(request.form.get('debt_rate') or request.form.get('interest_rate') or 0) / 100
            mgmt_options = float(request.form.get('mgmt_option_pct') or 0)

            # Validate debt capacity
            if debt_amount > company.debt_capacity:
                flash(f'Debt cannot exceed company capacity of ${company.debt_capacity:.1f}M.', 'danger')
                return redirect(request.url)

            stakes = [{'team_id': current_user.id,
                       'fund_id': lead_ts.fund_id,
                       'equity_invested': my_equity}]

            # Include selected fills
            selected_fills = request.form.getlist('selected_fills')
            for fill_ts_id in selected_fills:
                fts = TermSheet.query.get(int(fill_ts_id))
                if fts:
                    fill_equity = min(float(request.form.get(f'fill_equity_{fill_ts_id}',
                                                              fts.max_fill_equity)),
                                      fts.max_fill_equity)
                    stakes.append({'team_id': fts.team_id,
                                   'fund_id': fts.fund_id,
                                   'equity_invested': fill_equity})
                    fts.status = 'fill_accepted'
                    _notify(fts.team_id,
                            f'Your fill investment in {company.name} has been accepted!',
                            'deal_won', company.id)

            # Reject non-selected fills
            for fts in fill_offers:
                if str(fts.id) not in selected_fills:
                    fts.status = 'fill_declined'
                    _notify(fts.team_id,
                            f'Your fill offer on {company.name} was not included in the final deal.',
                            'deal_lost', company.id)

            deal.liquidation_preference = int(request.form.get('liquidation_preference',
                                                                lead_ts.liquidation_preference))
            deal.participation = 'participation' in request.form
            deal.anti_dilution = request.form.get('anti_dilution', lead_ts.anti_dilution)

            finalize_deal(deal, final_pre_money, stakes,
                          rolled_pct, debt_amount, debt_rate, mgmt_options)

            flash(f'Deal on {company.name} finalized successfully!', 'success')
            # Reputation boost for closing a deal
            current_user.reputation = min(5.0, current_user.reputation + 0.2)
            db.session.commit()
            return redirect(url_for('portfolio'))

        except (ValueError, KeyError) as e:
            flash(f'Error finalizing deal: {e}', 'danger')

    return render_template('phase2/finalize_deal.html',
                           game=game,
                           deal=deal,
                           company=company,
                           lead_ts=lead_ts,
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
    liquidated = (DealEquity.query
                  .filter_by(team_id=current_user.id)
                  .join(Deal, DealEquity.deal_id == Deal.id)
                  .filter(Deal.status.in_(['liquidated', 'bankrupt']))
                  .all())
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

    return render_template('portfolio/company.html',
                           game=game,
                           company=company,
                           deal=deal,
                           stake=stake,
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
    flash(f'{company.name} marked for liquidation with reserve price ${reserve_price:.1f}M.', 'success')
    return redirect(url_for('portfolio_company', company_id=company_id))


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------

@app.route('/funds')
@login_required
def funds():
    game = Game.query.get(current_user.game_id)
    team_funds = Fund.query.filter_by(team_id=current_user.id).all()
    transactions = (FundTransaction.query
                    .join(Fund, FundTransaction.fund_id == Fund.id)
                    .filter(Fund.team_id == current_user.id)
                    .order_by(FundTransaction.created_at.desc())
                    .limit(50).all())
    realized_irr = team_irr(current_user.id, game, unrealized=False)
    unrealized_irr = team_irr(current_user.id, game, unrealized=True)
    return render_template('funds.html',
                           game=game,
                           team_funds=team_funds,
                           transactions=transactions,
                           realized_irr=realized_irr,
                           unrealized_irr=unrealized_irr)


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
    return render_template('admin/dashboard.html',
                           game=game, teams=teams,
                           companies=companies, deals=deals)


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
        new_pw = request.form.get('new_password', '').strip()
        if new_pw:
            team.set_password(new_pw)

        # Fund adjustments
        for fund in team.funds:
            key = f'fund_cap_{fund.id}'
            if key in request.form:
                adj = float(request.form[key])
                fund.available_capital = adj

        db.session.commit()
        flash(f'{team.firm_name} updated.', 'success')
        return redirect(url_for('admin_teams'))
    return render_template('admin/edit_team.html', team=team, game=game)


SECTORS = ['Consumer', 'Energy', 'Healthcare', 'Industrials', 'Technology']
STAGES = ['startup', 'developing', 'early_revenue', 'mature']
FUND_SIZE_PARTNERS = {500: 5, 750: 8, 1000: 12}        # fund size ($M) -> total partners
FUND_SIZE_OPEX = {500: 0.01, 750: 0.008, 1000: 0.0075}  # fund size ($M) -> GP operating cost %/yr
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
    companies = GameCompany.query.filter_by(game_id=game.id).all() if game else []
    return render_template('admin/companies.html', game=game, companies=companies)


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
        fv = request.form.get('funded_valuation')
        company.funded_valuation = float(fv) if fv else None
        for y in range(1, GameCompany.MAX_TRACKED_YEARS + 1):
            yv = request.form.get(f'year_{y}_val')
            company.set_year_val(y, float(yv) if yv else None)
        company.status = request.form.get('status', company.status)
        company.management_quality = request.form.get('management_quality',
                                                       company.management_quality)
        company.is_cash_flow_positive = 'is_cash_flow_positive' in request.form

        if company.stage == 'mature':
            rg = request.form.get('revenue_growth_3yr')
            em = request.form.get('ltm_ebitda_margin')
            company.revenue_growth_3yr = float(rg) / 100 if rg else None
            company.ltm_ebitda_margin = float(em) / 100 if em else None

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

        if crank_type == 'phase1' and game.current_phase == 1:
            game.status = 'in_crank'
            db.session.commit()
            run_phase1_crank(game)
            flash(f'Deal Process complete. Year {game.current_year} Phase 2 is now open.', 'success')
        elif crank_type == 'phase2' and game.current_phase == 2:
            game.status = 'in_crank'
            db.session.commit()
            run_phase2_crank(game)
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
        u_irr = team_irr(team.id, game, unrealized=True)
        r_irr = team_irr(team.id, game, unrealized=False)
        total_deployed = sum(
            s.equity_invested for s in DealEquity.query.filter_by(team_id=team.id).all())
        portfolio_val = 0
        stakes = (DealEquity.query
                  .join(Deal, DealEquity.deal_id == Deal.id)
                  .filter(DealEquity.team_id == team.id, Deal.status == 'active')
                  .all())
        for s in stakes:
            val = s.deal.company.latest_valuation or 0
            portfolio_val += val * (s.ownership_pct / 100.0)

        gp_income = team_gp_income(team)
        team_data.append({
            'team': team,
            'unrealized_irr': u_irr,
            'realized_irr': r_irr,
            'total_capital': sum(f.total_capital for f in team.funds),
            'available_capital': team.total_available_capital,
            'deployed': total_deployed,
            'portfolio_value': portfolio_val,
            'reputation': team.reputation,
            'deal_count': len(stakes),
            'gp_income': gp_income,
        })

    team_data.sort(key=lambda x: x['unrealized_irr'], reverse=True)
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
