from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()

# EBITDA is not cash. A profitable company converts EBITDA to cash at 60%;
# a cash-burning company (negative EBITDA) consumes cash at 1.25x the burn.
EBITDA_CASH_YIELD = 0.60      # positive EBITDA -> cash
EBITDA_BURN_MULTIPLE = 1.25   # negative EBITDA -> cash burn


def ebitda_to_cash(ebitda):
    """Convert a year's EBITDA into the cash it actually generates/consumes.
    Used for MATURE (buyout) companies; venture companies use a burn rate."""
    if ebitda is None:
        return 0.0
    return ebitda * EBITDA_CASH_YIELD if ebitda >= 0 else ebitda * EBITDA_BURN_MULTIPLE


# Venture companies don't convert EBITDA to cash; they burn cash at a set rate.
# The starting burn = funds sought / a stage runway target, so a full raise buys
# that many years of runway (a smaller raise buys less). Mature cos don't burn.
BURN_RUNWAY_YEARS = {'startup': 2.5, 'developing': 3.0, 'early_revenue': 4.0}


def starting_burn_rate(stage, capital_requested):
    runway = BURN_RUNWAY_YEARS.get(stage)
    if not runway or not capital_requested:
        return 0.0
    return capital_requested / runway


# Revenue projection for venture companies. Revenue compounds off the authored
# 3-year growth rate, tapered each year so very high early growth doesn't run
# away over a long hold. Pre-revenue startups ($0 authored revenue) are seeded
# from the capital they raised. Used to size EBITDA if/when a distressed venture
# company turns profitable (EBITDA = projected revenue x a random margin).
STARTUP_REVENUE_SEED_FACTOR = 0.5   # seed revenue = this x capital raised
REVENUE_GROWTH_DECAY = 0.7          # each year's growth rate = prior x this


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, default='PE Simulation')
    current_year = db.Column(db.Integer, default=1)
    current_phase = db.Column(db.Integer, default=1)  # 1 or 2
    status = db.Column(db.String(20), default='active')  # active, paused, in_crank, completed
    # When on, the phase crank runs automatically once every team has marked the
    # current phase complete (see Team.ready_year/ready_phase) — no admin needed.
    auto_advance = db.Column(db.Boolean, default=True)
    market_condition = db.Column(db.Float, default=1.0)  # multiplier on outcome distributions
    query_points_per_year = db.Column(db.Integer, default=10)  # unused — searches are free
    total_years = db.Column(db.Integer, default=7)
    # Multi-game support. owner_id is the organizer who runs this game (wired up
    # in Phase 2 accounts; nullable for now). is_archived hides finished games
    # from the admin's active game switcher without deleting their data.
    owner_id = db.Column(db.Integer, nullable=True)
    is_archived = db.Column(db.Boolean, default=False)
    # Short shareable code for the public Join-a-Game page. Joining is only
    # allowed while the game is still at Year 1, Phase 1.
    join_code = db.Column(db.String(12), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teams = db.relationship('Team', backref='game', lazy=True)
    companies = db.relationship('GameCompany', backref='game', lazy=True)

    @property
    def phase_label(self):
        if self.status == 'completed':
            return f"Game Complete — Year {self.current_year} Final Results"
        return f"Year {self.current_year}, Phase {self.current_phase}"

    @property
    def is_joinable(self):
        """New teams may join only before the first Deal Process has run."""
        return (not self.is_archived and self.status == 'active'
                and self.current_year == 1 and self.current_phase == 1)

    @property
    def is_complete(self):
        return self.status == 'completed'


class Team(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    password_plain = db.Column(db.String(100), default='')  # shown to admin; classroom tool, not sensitive
    firm_name = db.Column(db.String(100), nullable=False)
    about_us = db.Column(db.Text, default='')
    reputation = db.Column(db.Float, default=5.0)  # 1–5 scale (currently unused in UI/logic)
    is_admin = db.Column(db.Boolean, default=False)
    query_points = db.Column(db.Integer, default=10)  # unused — searches are free
    sector_focus = db.Column(db.String(30), default='generalist')  # generalist or a sector name
    fund_type = db.Column(db.String(10), default='pe')             # 'vc' or 'pe'
    num_partners = db.Column(db.Integer, default=5)                # set by fund size at creation
    # "Done with this phase" signal. The team is ready for the CURRENT phase when
    # ready_year/ready_phase match the game's current year/phase; on any phase
    # advance the stored pair no longer matches, so readiness self-clears.
    ready_year = db.Column(db.Integer, nullable=True)
    ready_phase = db.Column(db.Integer, nullable=True)
    # Activity tracking: when the team last signed in, and the last time it made
    # any request (updated at most every few minutes to keep writes cheap).
    last_login = db.Column(db.DateTime, nullable=True)
    last_seen = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    funds = db.relationship('Fund', backref='team', lazy=True, cascade='all, delete-orphan')
    term_sheets = db.relationship('TermSheet', foreign_keys='TermSheet.team_id', backref='team', lazy=True, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='team', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)
        self.password_plain = pw

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def total_available_capital(self):
        return sum(f.available_capital for f in self.funds if f.is_active)

    @property
    def unread_notifications(self):
        return sum(1 for n in self.notifications if not n.is_read)

    @property
    def allowed_stages(self):
        """VC funds invest in startup/developing; PE funds in early_revenue/mature."""
        if self.fund_type == 'vc':
            return ('startup', 'developing')
        return ('early_revenue', 'mature')

    @property
    def fund_type_label(self):
        return 'Venture Capital' if self.fund_type == 'vc' else 'Private Equity'

    def investment_block_reason(self, company):
        """Return None if this team may invest in the company, else a human-readable reason."""
        if company.stage not in self.allowed_stages:
            return (f'{company.name} is a {company.stage_label} company. As a '
                    f'{self.fund_type_label} fund you can only invest in '
                    f'{" and ".join(s.replace("_", " ").title() for s in self.allowed_stages)} companies.')
        if self.sector_focus != 'generalist' and company.sector != self.sector_focus:
            return (f'{company.name} is in {company.sector}. Your fund mandate '
                    f'is limited to {self.sector_focus}.')
        return None

    def is_ready_for(self, game):
        """True if this team has marked the game's current phase complete."""
        return (self.ready_year == game.current_year
                and self.ready_phase == game.current_phase)


class Fund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    total_capital = db.Column(db.Float, nullable=False)   # $M committed by LPs
    available_capital = db.Column(db.Float, nullable=False)
    year_raised = db.Column(db.Integer, nullable=False, default=1)
    management_fee_rate = db.Column(db.Float, default=0.02)    # e.g. 0.02 = 2%
    performance_fee_rate = db.Column(db.Float, default=0.20)   # e.g. 0.20 = 20% carried interest
    operating_cost_rate = db.Column(db.Float, default=0.01)    # GP opex, % of committed capital/yr
    is_active = db.Column(db.Boolean, default=True)

    transactions = db.relationship('FundTransaction', backref='fund', lazy=True, cascade='all, delete-orphan')

    @property
    def deployed_capital(self):
        """Capital currently invested in ACTIVE holdings funded by this fund.
        Returns $0 once every deal has exited (e.g. at game end). This is the
        cash actually at work in deals — NOT committed-minus-cash, which would
        wrongly count management fees consumed over the fund's life."""
        return (db.session.query(
                    db.func.coalesce(db.func.sum(DealEquity.equity_invested), 0.0))
                .join(Deal, DealEquity.deal_id == Deal.id)
                .filter(DealEquity.fund_id == self.id, Deal.status == 'active')
                .scalar()) or 0.0

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
        """2x/3x liq pref, participation, and full anti-dilution not available for mature companies."""
        return self.stage in ('startup', 'developing', 'early_revenue')


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
    initial_val_ask = db.Column(db.Float)              # pre-money ask while listed
    funded_valuation = db.Column(db.Float)             # post-money at deal close
    year_1_val = db.Column(db.Float, nullable=True)    # valuation after each year's crank
    year_2_val = db.Column(db.Float, nullable=True)
    year_3_val = db.Column(db.Float, nullable=True)
    year_4_val = db.Column(db.Float, nullable=True)
    year_5_val = db.Column(db.Float, nullable=True)
    year_6_val = db.Column(db.Float, nullable=True)
    year_7_val = db.Column(db.Float, nullable=True)
    year_ebitdas = db.Column(db.Text, default='{}')   # JSON {year: ebitda} per crank
    year_burns = db.Column(db.Text, default='{}')     # JSON {year: burn} per crank (venture)
    year_mgmt_fees = db.Column(db.Text, default='{}') # JSON {year: total mgmt-change fees}
    year_followons = db.Column(db.Text, default='{}') # JSON {year: total follow-on cash injected}
    year_dividends = db.Column(db.Text, default='{}') # JSON {year: total dividends paid out of cash}
    year_returns = db.Column(db.Text, default='{}')   # JSON {year: market roll multiple} per crank
    year_revenues = db.Column(db.Text, default='{}')  # JSON {year: projected revenue} (venture)
    turned_profitable = db.Column(db.Boolean, default=False)  # venture recovered to positive EBITDA
    year_available = db.Column(db.Integer, default=1)
    year_funded = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(30), default='available')
    # available, funded, liquidated, bankrupt
    lead_team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    liquidation_proceeds = db.Column(db.Float, nullable=True)
    company_funds = db.Column(db.Float, default=0.0)  # cash the company holds
    reasons_for_funding = db.Column(db.String(200), nullable=True)
    available_cash = db.Column(db.Float, default=0.0)
    founder_shares = db.Column(db.Integer, default=10000000)
    management_option_pct = db.Column(db.Float, default=0.10)
    in_distress = db.Column(db.Boolean, default=False)
    ever_distressed = db.Column(db.Boolean, default=False)  # ran out of cash at least once
    flagged_for_liquidation = db.Column(db.Boolean, default=False)
    # How a previously-distressed holding was resolved at the most recent crank,
    # and the game year it happened — drives the "what happened last period"
    # recap. One of: 'bankrupt', 'recovered', 'still_burning'.
    distress_resolution = db.Column(db.String(20), nullable=True)
    distress_resolution_year = db.Column(db.Integer, nullable=True)
    revenue_growth_3yr = db.Column(db.Float, nullable=True)   # e.g. 0.12 = 12%
    ltm_ebitda_margin = db.Column(db.Float, nullable=True)    # e.g. 0.20 = 20%
    ltm_revenue = db.Column(db.Float, nullable=True)          # $M
    ltm_ebitda = db.Column(db.Float, nullable=True)           # $M (negative = burn)
    annual_burn_rate = db.Column(db.Float, default=0.0)       # $M/yr cash burn (venture)

    template = db.relationship('CompanyTemplate')
    lead_team = db.relationship('Team', foreign_keys=[lead_team_id])
    term_sheets = db.relationship('TermSheet', foreign_keys='TermSheet.company_id',
                                   backref='company', lazy=True, cascade='all, delete-orphan')
    deal = db.relationship('Deal', backref='company', uselist=False,
                            foreign_keys='Deal.company_id', cascade='all, delete-orphan')
    searches = db.relationship('CompanySearch', backref='company', lazy=True, cascade='all, delete-orphan')

    def get_outcomes(self):
        return json.loads(self.outcome_distributions) if self.outcome_distributions else []

    MAX_TRACKED_YEARS = 7

    def get_year_val(self, year):
        if 1 <= year <= self.MAX_TRACKED_YEARS:
            return getattr(self, f'year_{year}_val')
        return None

    def set_year_val(self, year, value):
        if 1 <= year <= self.MAX_TRACKED_YEARS:
            setattr(self, f'year_{year}_val', value)

    def adjust_latest_valuation(self, delta):
        """Shift the most recent valuation by `delta` (floored at $0), writing it
        back to whichever slot latest_valuation reads from so future cranks roll
        off the adjusted base. The valuation is assumed to incorporate the
        company's cash, so spending/injecting cash moves the mark one-for-one
        (mirror of how a follow-on injection raises it)."""
        for y in range(self.MAX_TRACKED_YEARS, 0, -1):
            cur = self.get_year_val(y)
            if cur is not None:
                self.set_year_val(y, max(0.0, cur + delta))
                return
        if self.funded_valuation is not None:
            self.funded_valuation = max(0.0, self.funded_valuation + delta)
        elif self.initial_val_ask is not None:
            self.initial_val_ask = max(0.0, self.initial_val_ask + delta)

    def get_year_ebitda(self, year):
        """EBITDA recorded for a given crank year (evolves with the return)."""
        data = json.loads(self.year_ebitdas) if self.year_ebitdas else {}
        return data.get(str(year))

    def set_year_ebitda(self, year, value):
        data = json.loads(self.year_ebitdas) if self.year_ebitdas else {}
        data[str(year)] = value
        self.year_ebitdas = json.dumps(data)

    def get_year_burn(self, year):
        """Cash burn recorded for a given crank year (evolves with the return)."""
        data = json.loads(self.year_burns) if self.year_burns else {}
        return data.get(str(year))

    def set_year_burn(self, year, value):
        data = json.loads(self.year_burns) if self.year_burns else {}
        data[str(year)] = value
        self.year_burns = json.dumps(data)

    def get_year_mgmt_fee(self, year):
        """Total management-change fees paid out of cash in a given year."""
        data = json.loads(self.year_mgmt_fees) if self.year_mgmt_fees else {}
        return data.get(str(year), 0.0)

    def add_year_mgmt_fee(self, year, amount):
        """Accumulate a management-change fee for the given year."""
        data = json.loads(self.year_mgmt_fees) if self.year_mgmt_fees else {}
        data[str(year)] = data.get(str(year), 0.0) + amount
        self.year_mgmt_fees = json.dumps(data)

    def get_year_followon(self, year):
        """Total follow-on cash injected into the company in a given year."""
        data = json.loads(self.year_followons) if self.year_followons else {}
        return data.get(str(year), 0.0)

    def add_year_followon(self, year, amount):
        """Accumulate a follow-on injection for the given year."""
        data = json.loads(self.year_followons) if self.year_followons else {}
        data[str(year)] = data.get(str(year), 0.0) + amount
        self.year_followons = json.dumps(data)

    def get_year_dividend(self, year):
        """Total dividends paid out of the company's cash in a given year."""
        data = json.loads(self.year_dividends) if self.year_dividends else {}
        return data.get(str(year), 0.0)

    def add_year_dividend(self, year, amount):
        """Accumulate a dividend payout for the given year."""
        data = json.loads(self.year_dividends) if self.year_dividends else {}
        data[str(year)] = data.get(str(year), 0.0) + amount
        self.year_dividends = json.dumps(data)

    def get_year_return(self, year):
        """Market roll multiple drawn for a given crank year (None if not
        recorded, e.g. a legacy crank that predates this column)."""
        data = json.loads(self.year_returns) if self.year_returns else {}
        return data.get(str(year))

    def set_year_return(self, year, multiple):
        data = json.loads(self.year_returns) if self.year_returns else {}
        data[str(year)] = multiple
        self.year_returns = json.dumps(data)

    def get_year_revenue(self, year):
        """Projected revenue recorded for a given crank year (venture)."""
        data = json.loads(self.year_revenues) if self.year_revenues else {}
        return data.get(str(year))

    def set_year_revenue(self, year, value):
        data = json.loads(self.year_revenues) if self.year_revenues else {}
        data[str(year)] = value
        self.year_revenues = json.dumps(data)

    def projected_revenue(self, year):
        """Projected revenue for `year`, compounding the authored 3-year growth
        rate from a base (authored revenue, or seeded from capital raised for a
        pre-revenue startup), tapering the growth rate each year held."""
        base = self.ltm_revenue if (self.ltm_revenue or 0) > 0 \
            else STARTUP_REVENUE_SEED_FACTOR * (self.capital_requested or 0.0)
        rev = base
        g0 = self.revenue_growth_3yr or 0.0
        for n in range(1, (year - (self.year_funded or year)) + 1):
            rev *= (1.0 + g0 * (REVENUE_GROWTH_DECAY ** (n - 1)))
        return rev

    @property
    def latest_valuation(self):
        """Most recent known valuation: latest year val, else funded, else initial ask."""
        for y in range(self.MAX_TRACKED_YEARS, 0, -1):
            v = getattr(self, f'year_{y}_val')
            if v is not None:
                return v
        if self.funded_valuation is not None:
            return self.funded_valuation
        return self.initial_val_ask

    @property
    def annual_operating_cash(self):
        """Cash the company generates/consumes a year. Mature (and a venture that
        has turned profitable): EBITDA converted to cash (60% of a profit).
        Pre-profit venture: negative of its annual burn rate."""
        if self.stage == 'mature' or self.turned_profitable:
            return ebitda_to_cash(self.ltm_ebitda)
        return -(self.annual_burn_rate or 0.0)

    @property
    def net_annual_cash_flow(self):
        """Operating cash less interest on outstanding debt (interest-only).
        Positive => the company generates cash after debt service."""
        return (self.annual_operating_cash
                - (self.debt_outstanding or 0) * (self.debt_interest_rate or 0))

    @property
    def enterprise_value(self):
        """Implied EV: equity value (latest valuation / ask) + debt - cash."""
        if self.latest_valuation is None:
            return None
        return (self.latest_valuation + (self.debt_outstanding or 0)
                - (self.available_cash or 0))

    @property
    def ev_revenue_multiple(self):
        ev = self.enterprise_value
        if ev is None or not self.ltm_revenue or self.ltm_revenue <= 0:
            return None
        return ev / self.ltm_revenue

    @property
    def ev_ebitda_multiple(self):
        """None when EBITDA is zero/negative (multiple not meaningful)."""
        ev = self.enterprise_value
        if ev is None or not self.ltm_ebitda or self.ltm_ebitda <= 0:
            return None
        return ev / self.ltm_ebitda

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
        return self.stage in ('startup', 'developing', 'early_revenue')

    @property
    def status_badge(self):
        colors = {
            'available': 'success',
            'funded': 'primary',
            'liquidated': 'secondary',
            'bankrupt': 'danger'
        }
        return colors.get(self.status, 'secondary')


class ReturnAssumption(db.Model):
    """Expected annual return and std dev by sector and stage."""
    __tablename__ = 'return_assumption'
    id = db.Column(db.Integer, primary_key=True)
    sector = db.Column(db.String(50), nullable=False)
    stage = db.Column(db.String(30), nullable=False)
    expected_return = db.Column(db.Float, default=0.10)   # e.g. 0.10 = 10%
    std_dev = db.Column(db.Float, default=0.20)            # e.g. 0.20 = 20%
    __table_args__ = (db.UniqueConstraint('sector', 'stage', name='uq_sector_stage'),)


class CompanySearch(db.Model):
    """Tracks which teams have found or been referred which companies."""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('game_company.id'), nullable=False)
    game_year = db.Column(db.Integer, nullable=False)
    found_by_search = db.Column(db.Boolean, default=True)
    on_watchlist = db.Column(db.Boolean, default=False)  # team explicitly saved it
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

    # Fill / co-invest terms
    willing_to_fill = db.Column(db.Boolean, default=True)
    min_lead_reputation = db.Column(db.Float, default=0.0)
    max_fill_equity = db.Column(db.Float, default=0.0)
    # When the lead invites this team as a co-investor at finalization, the
    # amount they propose (awaiting the team's accept/reject); None until offered.
    proposed_coinvest_amount = db.Column(db.Float, nullable=True)

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
    # pending, lead, fill_offered, coinvest_offered, fill_accepted,
    # fill_declined, rejected, withdrawn
    rejection_reason = db.Column(db.String(300), nullable=True)  # set when status -> rejected
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
        # Shortfall penalty: up to -3 pts if team covers none of the funds wanted
        capital_requested = self.company.capital_requested if self.company else 0
        if capital_requested > 0:
            shortfall_pct = max(0, (capital_requested - self.total_investment) / capital_requested)
            score -= shortfall_pct * 3
        # Rollover is no longer a competitive term: venture founders always roll
        # 100% and buyout sellers always cash out 100%, so it never differentiates
        # two offers and carries no scoring weight.
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
    # Lead's explicit choice, while distressed, to forgo a rescue and let the
    # turn-profitable roll decide (non-mature). Reset when a new distress begins.
    let_it_roll = db.Column(db.Boolean, default=False)

    lead_team = db.relationship('Team')
    equity_stakes = db.relationship('DealEquity', backref='deal', lazy=True, cascade='all, delete-orphan')
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

    @property
    def current_value(self):
        """Marked value of this equity stake: the company's *equity* value
        (its latest/enterprise valuation net of outstanding debt) times this
        stake's ownership. Matches the exit waterfall and IRR, which pay off
        debt before distributing to equity — so a leveraged deal isn't valued
        as if equity owned the debt-funded portion too."""
        company = self.deal.company
        equity_val = max(0.0, (company.latest_valuation or 0)
                         - (company.debt_outstanding or 0))
        return equity_val * (self.ownership_pct / 100.0)


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


class GameSetting(db.Model):
    """Admin-tunable simulation constant (Game Dynamics page).

    Stores an override for a named module-level constant. Defaults live in
    code; only changed values are persisted. game_settings.apply_overrides()
    reads these and writes them onto the live module globals at startup and
    after each save, so future cranks/rolls pick them up.
    """
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Float, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
