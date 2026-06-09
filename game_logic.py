"""
Core game logic: crank algorithms, IRR calculation, lead selection.
"""
import random
import math
from datetime import datetime
from models import (db, Game, Team, Fund, GameCompany, CompanySearch,
                    TermSheet, Deal, DealEquity, FundTransaction, Notification)


# ---------------------------------------------------------------------------
# Lead Selection Algorithm (Phase 1 Crank)
# ---------------------------------------------------------------------------

def run_phase1_crank(game: Game):
    """
    For each company that has pending term sheets, select a Lead Investor.
    Compatible fill investors are identified and notified.
    """
    companies_with_bids = (
        GameCompany.query
        .filter_by(game_id=game.id, status='available')
        .join(TermSheet, TermSheet.company_id == GameCompany.id)
        .filter(TermSheet.game_year == game.current_year,
                TermSheet.status == 'pending')
        .distinct()
        .all()
    )

    for company in companies_with_bids:
        term_sheets = (
            TermSheet.query
            .filter_by(company_id=company.id,
                       game_year=game.current_year,
                       status='pending')
            .all()
        )
        if not term_sheets:
            continue

        # Score each term sheet from the company's perspective
        scored = []
        for ts in term_sheets:
            lead_team = Team.query.get(ts.team_id)
            score = ts.company_score + (lead_team.reputation * 0.5)
            scored.append((score, ts))

        # Sort descending — highest score wins lead
        scored.sort(key=lambda x: x[0], reverse=True)
        lead_score, lead_ts = scored[0]
        lead_team = Team.query.get(lead_ts.team_id)

        # Mark lead
        lead_ts.status = 'lead'

        # Find compatible fills (willing_to_fill, compatible terms, reputation threshold)
        for score, ts in scored[1:]:
            if not ts.willing_to_fill:
                ts.status = 'rejected'
                _notify(ts.team_id,
                        f"Your term sheet on {company.name} was not selected as lead.",
                        'deal_lost', company.id)
                continue

            team = Team.query.get(ts.team_id)
            if team.reputation < ts.min_lead_reputation:
                # Fill's own threshold wasn't met by lead
                ts.status = 'rejected'
                _notify(ts.team_id,
                        f"Your term sheet on {company.name} was not selected.",
                        'deal_lost', company.id)
                continue

            # Check term compatibility with lead
            compatible = _terms_compatible(lead_ts, ts)
            if compatible:
                ts.status = 'fill_offered'
                _notify(ts.team_id,
                        f"You have been offered a Fill position on {company.name}. "
                        f"The Lead Investor is {lead_team.firm_name}.",
                        'fill_offered', company.id)
            else:
                ts.status = 'fill_offered'   # still offered; lead can accept revised terms
                _notify(ts.team_id,
                        f"You have been offered a Fill position on {company.name}, "
                        f"but your terms may be incompatible. You may submit a revised term sheet.",
                        'fill_offered', company.id)

        # Notify lead
        _notify(lead_ts.team_id,
                f"Congratulations! Your firm has been selected as Lead Investor on {company.name}. "
                f"Please finalize the deal in Phase 2.",
                'deal_won', company.id)

        # Update company status
        company.status = 'funded'
        company.lead_team_id = lead_ts.team_id

        # Create a pending Deal record for Phase 2 finalization
        deal = Deal(
            company_id=company.id,
            lead_team_id=lead_ts.team_id,
            lead_term_sheet_id=lead_ts.id,
            game_year=game.current_year,
            pre_money_valuation=lead_ts.pre_money_valuation,
            post_money_valuation=lead_ts.pre_money_valuation + lead_ts.total_investment,
            total_equity_invested=lead_ts.total_investment,
            rolled_equity_pct=(lead_ts.rolled_equity_min + lead_ts.rolled_equity_max) / 2,
            liquidation_preference=lead_ts.liquidation_preference,
            participation=lead_ts.participation,
            anti_dilution=lead_ts.anti_dilution,
            status='pending_finalization',
        )
        db.session.add(deal)

    # Advance to Phase 2
    game.current_phase = 2
    game.status = 'active'
    db.session.commit()


def _terms_compatible(lead_ts: TermSheet, fill_ts: TermSheet) -> bool:
    """Check if fill terms are compatible with lead terms."""
    if fill_ts.liquidation_preference > lead_ts.liquidation_preference:
        return False
    if fill_ts.participation and not lead_ts.participation:
        return False
    return True


# ---------------------------------------------------------------------------
# Phase 2 Crank (Year Advance)
# ---------------------------------------------------------------------------

def run_phase2_crank(game: Game):
    """
    - Charge management fees
    - Simulate company performance
    - Process debt payments
    - Process dividends
    - Process liquidations
    - Reset query points; advance to next year Phase 1
    """
    year = game.current_year

    # 1. Management fees (2% of total fund committed capital)
    for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
        for fund in team.funds:
            if not fund.is_active:
                continue
            fee = fund.total_capital * fund.management_fee_rate
            fund.available_capital = max(0, fund.available_capital - fee)
            _record_transaction(fund.id, 'management_fee', -fee,
                                f"Year {year} management fee", year)

    # 2. Simulate company performance for all active deals
    active_deals = (
        Deal.query
        .join(GameCompany, Deal.company_id == GameCompany.id)
        .filter(GameCompany.game_id == game.id, Deal.status == 'active')
        .all()
    )

    for deal in active_deals:
        company = deal.company

        # Roll outcome
        multiple = _roll_outcome(company, game.market_condition)
        old_val = company.current_valuation or company.post_money_valuation or 10.0
        new_val = old_val * multiple
        company.current_valuation = max(0.0, new_val)

        # Debt payment
        if company.debt_outstanding > 0 and company.debt_years_remaining > 0:
            annual_payment = company.debt_outstanding / company.debt_years_remaining
            interest = company.debt_outstanding * company.debt_interest_rate
            company.debt_outstanding = max(0, company.debt_outstanding - annual_payment)
            company.debt_years_remaining -= 1
            company.company_funds = max(0, company.company_funds - annual_payment - interest)

        # Check distress
        if company.current_valuation <= 0 or company.company_funds < 0:
            _process_bankruptcy(deal, company, year)
            continue

        # Liquidation check
        if deal.marked_for_liquidation:
            _process_liquidation(deal, company, year)

    # 3. Reset Phase 1 query points
    for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
        team.query_points = game.query_points_per_year

    # 4. Advance year
    game.current_year += 1
    game.current_phase = 1
    game.status = 'active'
    db.session.commit()

    # 5. Notify all teams
    for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
        _notify(team.id,
                f"Year {year} has ended. Year {game.current_year} Phase 1 is now open.",
                'crank_complete')


def _roll_outcome(company: GameCompany, market_condition: float) -> float:
    """Pick a random outcome multiple based on the company's distribution."""
    outcomes = company.get_outcomes()
    if not outcomes:
        return 1.0
    rnd = random.random()
    cumulative = 0.0
    for o in outcomes:
        cumulative += o['prob']
        if rnd <= cumulative:
            return o['multiple'] * market_condition
    return outcomes[-1]['multiple'] * market_condition


def _process_bankruptcy(deal: Deal, company: GameCompany, year: int):
    company.status = 'bankrupt'
    deal.status = 'bankrupt'
    for stake in deal.equity_stakes:
        _notify(stake.team_id,
                f"{company.name} has gone bankrupt. Your investment has been lost.",
                'liquidation', company.id)
    db.session.commit()


def _process_liquidation(deal: Deal, company: GameCompany, year: int):
    """Distribute proceeds according to liquidation waterfall."""
    proceeds = company.current_valuation
    reserve = deal.reserve_price or 0

    if proceeds < reserve:
        # Company stays in portfolio; sale didn't happen
        company.status = 'funded'
        deal.marked_for_liquidation = False
        return

    company.status = 'liquidated'
    deal.status = 'liquidated'
    company.liquidation_proceeds = proceeds
    remaining = proceeds

    # 1. Pay off debt first
    if company.debt_outstanding > 0:
        remaining = max(0, remaining - company.debt_outstanding)

    # 2. Liquidation preference
    total_invested = deal.total_equity_invested
    liq_pref_amount = total_invested * deal.liquidation_preference

    # Determine investor payout per stake
    investor_ownership = sum(s.ownership_pct for s in deal.equity_stakes)

    if remaining >= liq_pref_amount:
        # Preferred investors take liq pref; rest to common (founders)
        # If participation: investors also get pro-rata of remainder
        investor_payout = liq_pref_amount
        remainder_after_pref = remaining - liq_pref_amount
        if deal.participation:
            # Investors also participate in remaining on pro-rata basis
            investor_payout += remainder_after_pref * (investor_ownership / 100.0)
        else:
            # Check if converting to common gives more
            common_payout = remaining * (investor_ownership / 100.0)
            investor_payout = max(investor_payout, common_payout)
    else:
        investor_payout = remaining  # all goes to investors (preference)

    # Distribute to each equity holder proportionally
    for stake in deal.equity_stakes:
        stake_share = investor_payout * (stake.ownership_pct / investor_ownership) if investor_ownership > 0 else 0
        fund = Fund.query.get(stake.fund_id)
        fund.available_capital += stake_share
        _record_transaction(stake.fund_id, 'liquidation_proceeds', stake_share,
                            f"Liquidation of {company.name}", year, company.id)
        _notify(stake.team_id,
                f"{company.name} has been liquidated for ${proceeds:.1f}M. "
                f"Your share: ${stake_share:.2f}M.",
                'liquidation', company.id)

    db.session.commit()


# ---------------------------------------------------------------------------
# IRR Calculation
# ---------------------------------------------------------------------------

def calculate_irr(cash_flows: list) -> float:
    """
    Simple IRR via Newton-Raphson.
    cash_flows: list of (year, amount) tuples; negative = outflow, positive = inflow.
    Returns IRR as a decimal (e.g., 0.25 = 25%).
    """
    if not cash_flows:
        return 0.0

    def npv(rate):
        return sum(cf / ((1 + rate) ** yr) for yr, cf in cash_flows)

    def npv_deriv(rate):
        return sum(-yr * cf / ((1 + rate) ** (yr + 1)) for yr, cf in cash_flows)

    rate = 0.1
    for _ in range(200):
        f = npv(rate)
        df = npv_deriv(rate)
        if df == 0:
            break
        rate -= f / df
        if abs(f) < 1e-6:
            break

    return round(rate, 4)


def team_irr(team_id: int, game: Game, unrealized: bool = False):
    """
    Build cash flows for a team across all funds and calculate IRR.
    If unrealized=True, includes current portfolio value as a terminal cash flow.
    """
    transactions = (
        FundTransaction.query
        .join(Fund, FundTransaction.fund_id == Fund.id)
        .filter(Fund.team_id == team_id)
        .all()
    )

    # Group by year
    flows_by_year = {}
    for tx in transactions:
        flows_by_year[tx.game_year] = flows_by_year.get(tx.game_year, 0) + tx.amount

    if unrealized:
        # Add unrealized portfolio value as current-year inflow
        portfolio_value = 0.0
        stakes = (
            DealEquity.query
            .join(Deal, DealEquity.deal_id == Deal.id)
            .filter(DealEquity.team_id == team_id, Deal.status == 'active')
            .all()
        )
        for stake in stakes:
            company = stake.deal.company
            val = (company.current_valuation or company.post_money_valuation or 0)
            debt = company.debt_outstanding or 0
            net_val = max(0, val - debt)
            portfolio_value += net_val * (stake.ownership_pct / 100.0)

        flows_by_year[game.current_year] = (
            flows_by_year.get(game.current_year, 0) + portfolio_value
        )

    if not flows_by_year:
        return 0.0

    cf_list = sorted(flows_by_year.items())
    # Normalize to year 0
    base_year = cf_list[0][0]
    normalized = [(yr - base_year, amt) for yr, amt in cf_list]
    return calculate_irr(normalized)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notify(team_id, message, notification_type, company_id=None):
    n = Notification(
        team_id=team_id,
        message=message,
        notification_type=notification_type,
        related_company_id=company_id
    )
    db.session.add(n)


def _record_transaction(fund_id, tx_type, amount, description, year, company_id=None):
    tx = FundTransaction(
        fund_id=fund_id,
        transaction_type=tx_type,
        amount=amount,
        description=description,
        game_year=year,
        company_id=company_id
    )
    db.session.add(tx)


def finalize_deal(deal: Deal, final_pre_money: float, equity_stakes_data: list,
                  rolled_equity_pct: float, debt_amount: float = 0.0,
                  debt_rate: float = 0.0, mgmt_option_pct: float = 0.0):
    """
    equity_stakes_data: [{'team_id':..., 'fund_id':..., 'equity_invested':...}, ...]
    """
    total_equity = sum(s['equity_invested'] for s in equity_stakes_data)
    post_money = final_pre_money + total_equity + debt_amount

    deal.pre_money_valuation = final_pre_money
    deal.post_money_valuation = post_money
    deal.total_equity_invested = total_equity
    deal.rolled_equity_pct = rolled_equity_pct
    deal.debt_amount = debt_amount
    deal.debt_interest_rate = debt_rate
    deal.mgmt_option_pct = mgmt_option_pct
    deal.status = 'active'
    deal.finalized_at = datetime.utcnow()

    # Founders own rolled_equity_pct of post-money
    # Investor equity = (1 - rolled_equity_pct) allocated among investors
    investor_pool_pct = (1.0 - rolled_equity_pct) * 100.0  # total % available to investors

    for s in equity_stakes_data:
        ownership = (s['equity_invested'] / total_equity) * investor_pool_pct if total_equity > 0 else 0
        # Adjust for management options
        ownership_after_mgmt = ownership * (1.0 - mgmt_option_pct / 100.0)

        stake = DealEquity(
            deal_id=deal.id,
            team_id=s['team_id'],
            fund_id=s['fund_id'],
            equity_invested=s['equity_invested'],
            ownership_pct=round(ownership_after_mgmt, 4),
            is_lead=(s['team_id'] == deal.lead_team_id)
        )
        db.session.add(stake)

        # Deduct from fund
        fund = Fund.query.get(s['fund_id'])
        fund.available_capital -= s['equity_invested']
        _record_transaction(fund.id, 'investment', -s['equity_invested'],
                            f"Investment in {deal.company.name}",
                            deal.game_year, deal.company_id)

    # Update company
    company = deal.company
    company.post_money_valuation = post_money
    company.current_valuation = post_money
    company.debt_outstanding = debt_amount
    company.debt_interest_rate = debt_rate
    company.debt_years_remaining = 3 if debt_amount > 0 else 0
    company.company_funds = total_equity + debt_amount
    company.year_funded = deal.game_year

    db.session.commit()
