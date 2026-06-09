from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, default='PE Simulation')
    current_year = db.Column(db.Integer, default=1)
    current_phase = db.Column(db.Integer, default=1)  # 1 or 2
    status = db.Column(db.String(20), default='active')  # active, paused, in_crank
    market_condition = db.Column(db.Float, default=1.0)  # multiplier on outcome distributions
    query_points_per_year = db.Column(db.Integer, default=10)
    total_years = db.Column(db.Integer, default=7)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teams = db.relationship('Team', backref='game', lazy=True)
    companies = db.relationship('GameCompany', backref='game', lazy=True)

    @property
    def phase_label(self):
        return f"Year {self.current_year}, Phase {self.current_phase}"


class Team(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    firm_name = db.Column(db.String(100), nullable=False)
    about_us = db.Column(db.Text, default='')
    reputation = db.Column(db.Float, default=2.0)  # 1–5 scale
    is_admin = db.Column(db.Boolean, default=False)
    query_points = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    funds = db.relationship('Fund', backref='team', lazy=True)
    term_sheets = db.relationship('TermSheet', foreign_keys='TermSheet.team_id', backref='team', lazy=True)
    notifications = db.relationship('Notification', backref='team', lazy=True)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def total_available_capital(self):
        return sum(f.available_capital for f in self.funds if f.is_active)

    @property
    def unread_notifications(self):
        return sum(1 for n in self.notifications if not n.is_read)


class Fund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    total_capital = db.Column(db.Float, nullable=False)   # $M committed by LPs
    available_capital = db.Column(db.Float, nullable=False)
    year_raised = db.Column(db.Integer, nullable=False, default=1)
    management_fee_rate = db.Column(db.Float, default=0.02)
    is_active = db.Column(db.Boolean, default=True)

    transactions = db.relationship('FundTransaction', backref='fund', lazy=True)

    @property
    def deployed_capital(self):
        return self.total_capital - self.available_capital

    @property
    def deployment_pct(self):
        if self.total_capital == 0:
            return 0
        return round((self.deployed_capital / self.total_capital) * 100, 1)


class CompanyTemplate(db.Model):
    """Library of pre-loaded company definitions (not game-specific)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    sector = db.Column(db.String(50), nullable=False)
    stage = db.Column(db.String(30), nullable=False)       # startup, developing, early_revenue, mature
    description = db.Column(db.Text)
    capital_requested = db.Column(db.Float, nullable=False)  # $M
    rolled_equity_min = db.Column(db.Float, default=0.75)
    rolled_equity_max = db.Column(db.Float, default=1.0)
    debt_capacity = db.Column(db.Float, default=0.0)        # $M
    is_cash_flow_positive = db.Column(db.Boolean, default=False)
    dividend_eligible = db.Column(db.Boolean, default=False)
    management_quality = db.Column(db.String(20), default='average')  # strong, average, weak
    # JSON: [{"multiple": 0.5, "prob": 0.2}, {"multiple": 1.0, "prob": 0.4}, ...]
    outcome_distributions = db.Column(db.Text, nullable=False)
    base_valuation = db.Column(db.Float, nullable=False)    # suggested pre-money
    year_available = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)

    def get_outcomes(self):
        return json.loads(self.outcome_distributions)

    @property
    def expected_multiple(self):
        outcomes = self.get_outcomes()
        return sum(o['multiple'] * o['prob'] for o in outcomes)

    @property
    def stage_label(self):
        labels = {
            'startup': 'Startup',
            'developing': 'Developing',
            'early_revenue': 'Early Revenue',
            'mature': 'Mature'
        }
        return labels.get(self.stage, self.stage.title())

    @property
    def allows_aggressive_terms(self):
        """2x/3x liq pref, participation, and full anti-dilution only for startup/developing."""
        return self.stage in ('startup', 'developing')


class GameCompany(db.Model):
    """A company instance within a specific game."""
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('company_template.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    sector = db.Column(db.String(50), nullable=False)
    stage = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text)
    capital_requested = db.Column(db.Float, nullable=False)
    rolled_equity_min = db.Column(db.Float, default=0.75)
    rolled_equity_max = db.Column(db.Float, default=1.0)
    debt_capacity = db.Column(db.Float, default=0.0)
    debt_outstanding = db.Column(db.Float, default=0.0)
    debt_years_remaining = db.Column(db.Integer, default=0)
    debt_interest_rate = db.Column(db.Float, default=0.0)
    is_cash_flow_positive = db.Column(db.Boolean, default=False)
    dividend_eligible = db.Column(db.Boolean, default=False)
    management_quality = db.Column(db.String(20), default='average')
    outcome_distributions = db.Column(db.Text, nullable=False)
    current_valuation = db.Column(db.Float)           # updated each crank
    post_money_valuation = db.Column(db.Float)         # set after deal closes
    year_available = db.Column(db.Integer, default=1)
    year_funded = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(30), default='available')
    # available, funded, refinancing, liquidated, bankrupt
    lead_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    liquidation_proceeds = db.Column(db.Float, nullable=True)
    company_funds = db.Column(db.Float, default=0.0)  # cash the company holds

    template = db.relationship('CompanyTemplate')
    term_sheets = db.relationship('TermSheet', foreign_keys='TermSheet.company_id',
                                   backref='company', lazy=True)
    deal = db.relationship('Deal', backref='company', uselist=False,
                            foreign_keys='Deal.company_id')
    searches = db.relationship('CompanySearch', backref='company', lazy=True)

    def get_outcomes(self):
        return json.loads(self.outcome_distributions) if self.outcome_distributions else []

    @property
    def expected_multiple(self):
        outcomes = self.get_outcomes()
        return round(sum(o['multiple'] * o['prob'] for o in outcomes), 2)

    @property
    def stage_label(self):
        labels = {
            'startup': 'Startup',
            'developing': 'Developing',
            'early_revenue': 'Early Revenue',
            'mature': 'Mature'
        }
        return labels.get(self.stage, self.stage.title())

    @property
    def stage_badge(self):
        colors = {
            'startup': 'danger',
            'developing': 'warning',
            'early_revenue': 'info',
            'mature': 'success'
        }
        return colors.get(self.stage, 'secondary')

    @property
    def allows_aggressive_terms(self):
        return self.stage in ('startup', 'developing')

    @property
    def status_badge(self):
        colors = {
            'available': 'success',
            'funded': 'primary',
            'refinancing': 'info',
            'liquidated': 'secondary',
            'bankrupt': 'danger'
        }
        return colors.get(self.status, 'secondary')


class CompanySearch(db.Model):
    """Tracks which teams have found or been referred which companies."""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=False)
    game_year = db.Column(db.Integer, nullable=False)
    found_by_search = db.Column(db.Boolean, default=True)
    referred_by_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    team = db.relationship('Team', foreign_keys=[team_id])
    referring_team = db.relationship('Team', foreign_keys=[referred_by_team_id])


class TermSheet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=False)
    fund_id = db.Column(db.Integer, db.ForeignKey('fund.id'), nullable=False)
    game_year = db.Column(db.Integer, nullable=False)

    # Core valuation terms
    pre_money_valuation = db.Column(db.Float, nullable=False)
    total_investment = db.Column(db.Float, nullable=False)
    rolled_equity_min = db.Column(db.Float, nullable=False)
    rolled_equity_max = db.Column(db.Float, nullable=False)

    # Fill terms
    willing_to_fill = db.Column(db.Boolean, default=True)
    min_lead_reputation = db.Column(db.Float, default=0.0)
    max_fill_equity = db.Column(db.Float, default=0.0)

    # Protective provisions
    liquidation_preference = db.Column(db.Integer, default=1)   # 1, 2, or 3
    participation = db.Column(db.Boolean, default=False)
    anti_dilution = db.Column(db.String(20), default='none')     # none, weighted, full_ratchet

    # Syndicate
    term_sheet_type = db.Column(db.String(20), default='solo')   # solo, syndicate
    syndicate_partners = db.Column(db.Text, default='[]')        # JSON list of team_ids
    syndicate_approved_by = db.Column(db.Text, default='[]')     # JSON list of team_ids who approved

    # Result
    status = db.Column(db.String(30), default='pending')
    # pending, lead, fill_offered, fill_accepted, fill_declined, rejected, withdrawn
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    fund = db.relationship('Fund')

    def get_syndicate_partners(self):
        return json.loads(self.syndicate_partners)

    def get_syndicate_approved_by(self):
        return json.loads(self.syndicate_approved_by)

    @property
    def is_fully_approved(self):
        partners = self.get_syndicate_partners()
        approved = self.get_syndicate_approved_by()
        return all(p in approved for p in partners)

    @property
    def liq_pref_label(self):
        return f"{self.liquidation_preference}x"

    @property
    def anti_dilution_label(self):
        labels = {'none': 'None', 'weighted': 'Weighted Avg.', 'full_ratchet': 'Full Ratchet'}
        return labels.get(self.anti_dilution, self.anti_dilution)

    @property
    def company_score(self):
        """Higher = more attractive to company (used in lead selection)."""
        score = self.pre_money_valuation / 10  # normalize
        if self.liquidation_preference == 1:
            score += 2
        elif self.liquidation_preference == 2:
            score += 0
        else:
            score -= 2
        if not self.participation:
            score += 1
        if self.anti_dilution == 'none':
            score += 1
        elif self.anti_dilution == 'weighted':
            score += 0.5
        score += self.total_investment / 10
        return score


class Deal(db.Model):
    """A finalized investment."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=False)
    lead_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    lead_term_sheet_id = db.Column(db.Integer, db.ForeignKey('term_sheet.id'), nullable=True)
    game_year = db.Column(db.Integer, nullable=False)

    pre_money_valuation = db.Column(db.Float, nullable=False)
    post_money_valuation = db.Column(db.Float, nullable=False)
    total_equity_invested = db.Column(db.Float, nullable=False)
    rolled_equity_pct = db.Column(db.Float, nullable=False)
    debt_amount = db.Column(db.Float, default=0.0)
    debt_interest_rate = db.Column(db.Float, default=0.0)
    mgmt_option_pct = db.Column(db.Float, default=0.0)  # % granted to management

    liquidation_preference = db.Column(db.Integer, default=1)
    participation = db.Column(db.Boolean, default=False)
    anti_dilution = db.Column(db.String(20), default='none')

    status = db.Column(db.String(30), default='pending_finalization')
    # pending_finalization, active, liquidated, bankrupt
    finalized_at = db.Column(db.DateTime)
    reserve_price = db.Column(db.Float, nullable=True)   # liquidation reserve
    marked_for_liquidation = db.Column(db.Boolean, default=False)

    lead_team = db.relationship('Team')
    equity_stakes = db.relationship('DealEquity', backref='deal', lazy=True)
    lead_term_sheet = db.relationship('TermSheet')


class DealEquity(db.Model):
    """Each investor's stake in a deal."""
    id = db.Column(db.Integer, primary_key=True)
    deal_id = db.Column(db.Integer, db.ForeignKey('deal.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    fund_id = db.Column(db.Integer, db.ForeignKey('fund.id'), nullable=False)
    equity_invested = db.Column(db.Float, nullable=False)   # $ put in
    ownership_pct = db.Column(db.Float, nullable=False)      # % owned
    is_lead = db.Column(db.Boolean, default=False)

    team = db.relationship('Team')
    fund = db.relationship('Fund')


class FundTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fund_id = db.Column(db.Integer, db.ForeignKey('fund.id'), nullable=False)
    transaction_type = db.Column(db.String(50), nullable=False)
    # investment, management_fee, search_fee, dividend_received,
    # liquidation_proceeds, debt_interest_received, management_change_fee
    amount = db.Column(db.Float, nullable=False)   # positive = inflow, negative = outflow
    description = db.Column(db.String(300))
    game_year = db.Column(db.Integer, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    notification_type = db.Column(db.String(50))
    # deal_won, deal_lost, fill_offered, referral, crank_complete, liquidation
    related_company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    related_company = db.relationship('GameCompany')
