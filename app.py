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
                    FundTransaction, Notification)
from game_logic import (run_phase1_crank, run_phase2_crank,
                        team_irr, finalize_deal, _notify, _record_transaction)

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///simulategp.db')
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
             .order_by(Team.reputation.desc())
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


@app.route('/dealflow/search', methods=['GET', 'POST'])
@login_required
def search_companies():
    game = Game.query.get(current_user.game_id)
    if game.current_phase != 1:
        flash('Company search is only available in Phase 1.', 'warning')
        return redirect(url_for('dealflow'))

    sectors = db.session.query(GameCompany.sector).filter_by(
        game_id=game.id).distinct().all()
    sectors = [s[0] for s in sectors]

    results = []
    if request.method == 'POST':
        sector_filter = request.form.get('sector', '')
        stage_filter = request.form.get('stage', '')
        max_capital = request.form.get('max_capital', '')

        # Cost in query points
        cost = 1
        if sector_filter:
            cost = 1
        if stage_filter:
            cost += 0
        if max_capital:
            cost += 0

        if current_user.query_points < cost:
            # Charge search fee to fund
            fee = 0.1  # $0.1M per extra search
            primary_fund = Fund.query.filter_by(
                team_id=current_user.id, is_active=True).first()
            if primary_fund and primary_fund.available_capital >= fee:
                primary_fund.available_capital -= fee
                _record_transaction(primary_fund.id, 'search_fee', -fee,
                                    'Additional company search fee', game.current_year)
                db.session.commit()
                flash(f'Query points exhausted. Search fee of ${fee}M charged.', 'warning')
            else:
                flash('No query points remaining and insufficient funds to search.', 'danger')
                return redirect(url_for('dealflow'))
        else:
            current_user.query_points -= cost
            db.session.commit()

        # Build query
        query = GameCompany.query.filter_by(
            game_id=game.id, status='available').filter(
            GameCompany.year_available <= game.current_year)

        if sector_filter:
            query = query.filter(GameCompany.sector == sector_filter)
        if stage_filter:
            query = query.filter(GameCompany.stage == stage_filter)
        if max_capital:
            try:
                query = query.filter(
                    GameCompany.capital_requested <= float(max_capital))
            except ValueError:
                pass

        companies = query.all()

        # Already found companies
        already_found = set(
            s.company_id for s in CompanySearch.query.filter_by(
                team_id=current_user.id, game_year=game.current_year).all())

        for c in companies:
            if c.id not in already_found:
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

    return render_template('dealflow/company_detail.html',
                           game=game,
                           company=company,
                           existing_ts=existing_ts,
                           teams=teams,
                           funds=funds)


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

    funds = Fund.query.filter_by(team_id=current_user.id, is_active=True).all()
    teams = Team.query.filter_by(game_id=game.id, is_admin=False).filter(
        Team.id != current_user.id).all()

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
            max_fill = float(request.form.get('max_fill_equity', 0))
            min_rep = float(request.form.get('min_lead_reputation', 0))
            ts_type = request.form.get('term_sheet_type', 'solo')
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
                    f'You dropped the deal on {company.name}. Reputation affected.',
                    'deal_lost', company.id)
            db.session.commit()
            flash(f'Deal on {company.name} dropped.', 'warning')
            return redirect(url_for('timeline'))

        try:
            final_pre_money = float(request.form['pre_money_valuation'])
            # Validate: can't go below 90% of original bid
            min_allowed = lead_ts.pre_money_valuation * 0.90
            if final_pre_money < min_allowed:
                flash(f'Final valuation cannot be less than ${min_allowed:.1f}M '
                      f'(90% of your original bid).', 'danger')
                return redirect(request.url)

            my_equity = float(request.form['my_equity'])
            rolled_pct = float(request.form['rolled_equity_pct']) / 100
            debt_amount = float(request.form.get('debt_amount', 0))
            debt_rate = float(request.form.get('debt_rate', 0)) / 100
            mgmt_options = float(request.form.get('mgmt_option_pct', 0))

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

    cost = (company.current_valuation or 10.0) * 0.10
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
    if not company.dividend_eligible or not company.is_cash_flow_positive:
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


@app.route('/admin/setup', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_setup():
    if request.method == 'POST':
        # Create game
        game_name = request.form.get('game_name', 'PE Simulation')
        total_years = int(request.form.get('total_years', 7))
        qp = int(request.form.get('query_points', 10))
        starting_capital = float(request.form.get('starting_capital', 100.0))

        # Delete existing game data if re-setting up
        existing = Game.query.first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

        game = Game(name=game_name, total_years=total_years,
                    query_points_per_year=qp)
        db.session.add(game)
        db.session.flush()

        # Load companies from JSON
        companies_path = os.path.join(os.path.dirname(__file__), 'data', 'companies.json')
        with open(companies_path) as f:
            company_data = json.load(f)

        for cd in company_data:
            gc = GameCompany(
                game_id=game.id,
                name=cd['name'],
                sector=cd['sector'],
                stage=cd['stage'],
                description=cd['description'],
                capital_requested=cd['capital_requested'],
                rolled_equity_min=cd['rolled_equity_min'],
                rolled_equity_max=cd['rolled_equity_max'],
                debt_capacity=cd['debt_capacity'],
                is_cash_flow_positive=cd['is_cash_flow_positive'],
                dividend_eligible=cd.get('dividend_eligible', False),
                management_quality=cd['management_quality'],
                outcome_distributions=json.dumps(cd['outcome_distributions']),
                current_valuation=cd['base_valuation'],
                year_available=cd.get('year_available', 1)
            )
            db.session.add(gc)

        # Create teams from form
        team_count = int(request.form.get('team_count', 5))
        for i in range(1, team_count + 1):
            tname = request.form.get(f'team_{i}_name', f'Team {i}')
            tuname = request.form.get(f'team_{i}_username', f'team{i}')
            tpw = request.form.get(f'team_{i}_password', f'team{i}pass')
            team = Team(
                game_id=game.id,
                username=tuname,
                firm_name=tname,
                reputation=2.0,
                query_points=qp
            )
            team.set_password(tpw)
            db.session.add(team)
            db.session.flush()

            fund = Fund(
                team_id=team.id,
                name=f'{tname} Fund I',
                total_capital=starting_capital,
                available_capital=starting_capital,
                year_raised=1
            )
            db.session.add(fund)

        db.session.commit()
        flash(f'Game "{game_name}" created with {team_count} teams and '
              f'{len(company_data)} companies!', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/setup.html')


@app.route('/admin/teams')
@login_required
@admin_required
def admin_teams():
    game = Game.query.first()
    teams = Team.query.filter_by(game_id=game.id, is_admin=False).all() if game else []
    return render_template('admin/teams.html', game=game, teams=teams)


@app.route('/admin/team/<int:team_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_team(team_id):
    team = Team.query.get_or_404(team_id)
    game = Game.query.get(team.game_id)
    if request.method == 'POST':
        team.firm_name = request.form.get('firm_name', team.firm_name)
        team.reputation = float(request.form.get('reputation', team.reputation))
        team.query_points = int(request.form.get('query_points', team.query_points))
        new_pw = request.form.get('new_password', '').strip()
        if new_pw:
            team.set_password(new_pw)

        # Fund adjustments
        for fund in team.funds:
            key = f'fund_{fund.id}_capital'
            if key in request.form:
                adj = float(request.form[key])
                fund.available_capital = adj

        db.session.commit()
        flash(f'{team.firm_name} updated.', 'success')
        return redirect(url_for('admin_teams'))
    return render_template('admin/edit_team.html', team=team, game=game)


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
        company.current_valuation = float(request.form.get('current_valuation',
                                                            company.current_valuation))
        company.status = request.form.get('status', company.status)
        company.management_quality = request.form.get('management_quality',
                                                       company.management_quality)
        company.is_cash_flow_positive = 'is_cash_flow_positive' in request.form
        company.dividend_eligible = 'dividend_eligible' in request.form
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
        crank_type = request.form.get('crank_type')
        market_adj = float(request.form.get('market_condition', game.market_condition))
        game.market_condition = market_adj

        if crank_type == 'phase1' and game.current_phase == 1:
            game.status = 'in_crank'
            db.session.commit()
            run_phase1_crank(game)
            flash(f'Phase 1 Crank complete. Year {game.current_year} Phase 2 is now open.', 'success')
        elif crank_type == 'phase2' and game.current_phase == 2:
            game.status = 'in_crank'
            db.session.commit()
            run_phase2_crank(game)
            flash(f'Phase 2 Crank complete. Year {game.current_year} Phase 1 is now open.', 'success')
        else:
            flash('Invalid crank for current phase.', 'danger')

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
            val = s.deal.company.current_valuation or 0
            portfolio_val += val * (s.ownership_pct / 100.0)

        team_data.append({
            'team': team,
            'unrealized_irr': u_irr,
            'realized_irr': r_irr,
            'total_capital': sum(f.total_capital for f in team.funds),
            'available_capital': team.total_available_capital,
            'deployed': total_deployed,
            'portfolio_value': portfolio_val,
            'reputation': team.reputation,
            'deal_count': len(stakes)
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
                query_points=999,
                reputation=5.0
            )
            admin.set_password(os.environ.get('ADMIN_PASSWORD', 'admin123'))
            db.session.add(admin)
            db.session.commit()
            print("Admin user created: username=admin")


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
