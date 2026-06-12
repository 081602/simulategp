"""
Core game logic: crank algorithms, IRR calculation, lead selection.
"""
import random
import math
from datetime import datetime
from models import (db, Game, Team, Fund, GameCompany, CompanySearch,
                    TermSheet, Deal, DealEquity, FundTransaction, Notification,
                    ReturnAssumption)


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

        # The company declines offers from funds whose mandate doesn't
        # cover it (teams aren't warned up front — they find out here)
        in_mandate = []
        for ts in term_sheets:
            team = Team.query.get(ts.team_id)
            if team.investment_block_reason(company):
                ts.status = 'rejected'
                ts.rejection_reason = (
                    f"{company.name} declined to take capital from a fund whose "
                    f"mandate does not include {company.stage_label} "
                    f"{company.sector} companies.")
                _notify(ts.team_id,
                        f"{company.name} declined your term sheet. The company "
                        f"chose not to take capital from a fund whose mandate "
                        f"does not include {company.stage_label} {company.sector} "
                        f"companies.",
                        'deal_lost', company.id)
            else:
                in_mandate.append(ts)
        if not in_mandate:
            continue

        # Score each term sheet from the company's perspective
        # (reputation currently unused in scoring)
        scored = []
        for ts in in_mandate:
            score = ts.company_score
            scored.append((score, ts))

        # Sort descending — highest score wins lead
        scored.sort(key=lambda x: x[0], reverse=True)
        lead_score, lead_ts = scored[0]
        lead_team = Team.query.get(lead_ts.team_id)

        # Mark lead
        lead_ts.status = 'lead'

        # Find compatible fills (willing_to_fill, compatible terms)
        # (reputation threshold currently unused)
        for score, ts in scored[1:]:
            if not ts.willing_to_fill:
                ts.status = 'rejected'
                ts.rejection_reason = (
                    f"{company.name} selected {lead_team.firm_name}'s term sheet "
                    f"as lead; yours was not chosen and you did not offer to "
                    f"participate as a fill investor.")
                _notify(ts.team_id,
                        f"Your term sheet on {company.name} was not selected as lead.",
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
            # Buyouts: the price IS the company value; VC: price + new money
            post_money_valuation=(lead_ts.pre_money_valuation
                                  if company.stage == 'mature'
                                  else lead_ts.pre_money_valuation + lead_ts.total_investment),
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
        old_val = company.latest_valuation or 10.0
        new_val = old_val * multiple
        company.set_year_val(year, max(0.0, new_val))

        # Cash engine: EBITDA accrues (or burns) before debt service
        if company.ltm_ebitda is not None:
            company.company_funds += company.ltm_ebitda

        # Debt service
        if company.debt_outstanding > 0 and company.debt_years_remaining > 0:
            annual_payment = company.debt_outstanding / company.debt_years_remaining
            interest = company.debt_outstanding * company.debt_interest_rate
            company.debt_outstanding = max(0, company.debt_outstanding - annual_payment)
            company.debt_years_remaining -= 1
            company.company_funds -= (annual_payment + interest)

        # Valuation wipeout -> immediate bankruptcy
        if (company.latest_valuation or 0) <= 0:
            _process_bankruptcy(deal, company, year)
            continue

        # Cash exhausted: first year = distress warning, second year = bankrupt
        if company.company_funds < 0:
            if company.in_distress:
                _process_bankruptcy(deal, company, year)
                continue
            company.in_distress = True
            company.company_funds = 0.0
            for stake in deal.equity_stakes:
                _notify(stake.team_id,
                        f"{company.name} is in financial distress — cash exhausted. "
                        f"Without action it will go bankrupt next year.",
                        'distress', company.id)
        else:
            company.in_distress = False

        # Liquidation check
        if deal.marked_for_liquidation:
            _process_liquidation(deal, company, year)

    # 3. Advance year
    game.current_year += 1
    game.current_phase = 1
    game.status = 'active'
    db.session.commit()

    # 5. Notify all teams
    for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
        _notify(team.id,
                f"Year {year} has ended. Year {game.current_year} Phase 1 is now open.",
                'crank_complete')


# Stage-typical fundamentals: deviations from these tilt a company's return profile.
STAGE_TYPICAL_FUNDAMENTALS = {
    #                 (3yr revenue growth, LTM EBITDA margin)
    'startup':        (1.50, -0.60),
    'developing':     (0.50, -0.10),
    'early_revenue':  (0.45,  0.05),
    'mature':         (0.07,  0.18),
}
GROWTH_RETURN_WEIGHT = 0.10   # 10 pts of above-typical growth -> +1% expected return
MARGIN_RETURN_WEIGHT = 0.20   # 10 pts of above-typical margin -> +2% expected return
MAX_FUNDAMENTALS_TILT = 0.05  # cap on total expected-return shift (+/- 5%)
MARGIN_VOL_WEIGHT = 0.6       # 10 pts of above-typical margin -> -6% relative volatility
VOL_FACTOR_RANGE = (0.75, 1.25)
# Management quality tilts expected return (weak destroys more than strong adds)
MANAGEMENT_RETURN_TILT = {'strong': 0.02, 'average': 0.0, 'weak': -0.03}


def _fundamentals_adjustment(company: GameCompany, mu: float, sigma: float):
    """Tilt (mu, sigma) by how the company's growth/margin compare to stage-typical values.

    Above-typical revenue growth or EBITDA margin raises expected return;
    above-typical margin also dampens volatility (steadier businesses), and
    below-typical margin amplifies it. Companies without metrics are unaffected.
    """
    typical_growth, typical_margin = STAGE_TYPICAL_FUNDAMENTALS.get(
        company.stage, (0.20, 0.10))

    tilt = 0.0
    if company.revenue_growth_3yr is not None:
        tilt += GROWTH_RETURN_WEIGHT * (company.revenue_growth_3yr - typical_growth)
    if company.ltm_ebitda_margin is not None:
        tilt += MARGIN_RETURN_WEIGHT * (company.ltm_ebitda_margin - typical_margin)
    tilt = max(-MAX_FUNDAMENTALS_TILT, min(MAX_FUNDAMENTALS_TILT, tilt))

    vol_factor = 1.0
    if company.ltm_ebitda_margin is not None:
        vol_factor = 1.0 - MARGIN_VOL_WEIGHT * (company.ltm_ebitda_margin - typical_margin)
        vol_factor = max(VOL_FACTOR_RANGE[0], min(VOL_FACTOR_RANGE[1], vol_factor))

    return mu + tilt, sigma * vol_factor


def _roll_outcome(company: GameCompany, market_condition: float) -> float:
    """Sample annual return from N(expected_return, std_dev) for this company's
    sector/stage, tilted by fundamentals (growth, EBITDA margin) and
    management quality."""
    assumption = ReturnAssumption.query.filter_by(
        sector=company.sector, stage=company.stage
    ).first()
    if assumption:
        mu = assumption.expected_return
        sigma = assumption.std_dev
    else:
        mu, sigma = 0.10, 0.25
    mu, sigma = _fundamentals_adjustment(company, mu, sigma)
    mu += MANAGEMENT_RETURN_TILT.get(company.management_quality, 0.0)
    annual_return = random.gauss(mu, sigma)
    multiple = max(0.0, 1.0 + annual_return) * market_condition
    return multiple


def _process_bankruptcy(deal: Deal, company: GameCompany, year: int):
    company.status = 'bankrupt'
    deal.status = 'bankrupt'
    for stake in deal.equity_stakes:
        _notify(stake.team_id,
                f"{company.name} has gone bankrupt. Your investment has been lost.",
                'liquidation', company.id)
    db.session.commit()


def exit_waterfall(deal: Deal):
    """Read-only breakdown of how an exit's proceeds were (or would be)
    distributed — mirrors _process_liquidation step by step for display."""
    company = deal.company
    sale_price = company.liquidation_proceeds if company.liquidation_proceeds is not None \
        else (company.latest_valuation or 0)
    debt_repaid = min(sale_price, company.debt_outstanding or 0)
    distributable = max(0, sale_price - debt_repaid)

    invested = deal.total_equity_invested or 0
    pref_multiple = deal.liquidation_preference or 1
    liq_pref_amount = invested * pref_multiple
    investor_ownership = sum(s.ownership_pct for s in deal.equity_stakes)
    as_converted = distributable * (investor_ownership / 100.0)

    if distributable >= liq_pref_amount:
        if deal.participation:
            investor_payout = liq_pref_amount + \
                (distributable - liq_pref_amount) * (investor_ownership / 100.0)
            method = 'participation'
        elif as_converted > liq_pref_amount:
            investor_payout = as_converted
            method = 'converted'
        else:
            investor_payout = liq_pref_amount
            method = 'preference'
    else:
        investor_payout = distributable
        method = 'underwater'

    stakes = []
    for s in deal.equity_stakes:
        share = investor_payout * (s.ownership_pct / investor_ownership) \
            if investor_ownership > 0 else 0
        stakes.append({'team': s.team, 'ownership_pct': s.ownership_pct,
                       'invested': s.equity_invested, 'share': share})

    return {
        'sale_price': sale_price,
        'debt_repaid': debt_repaid,
        'distributable': distributable,
        'invested': invested,
        'pref_multiple': pref_multiple,
        'liq_pref_amount': liq_pref_amount,
        'investor_ownership': investor_ownership,
        'as_converted': as_converted,
        'participation': deal.participation,
        'investor_payout': investor_payout,
        'method': method,
        'founders_payout': max(0, distributable - investor_payout),
        'stakes': stakes,
    }


def _process_liquidation(deal: Deal, company: GameCompany, year: int):
    """Distribute proceeds according to liquidation waterfall."""
    proceeds = company.latest_valuation or 0
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
                            f"Exit of {company.name}", year, company.id)
        _notify(stake.team_id,
                f"{company.name} has been sold for ${proceeds:.1f}M. "
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

    # IRR needs at least one outflow and one inflow to be defined
    has_outflow = any(cf < 0 for _, cf in cash_flows)
    has_inflow = any(cf > 0 for _, cf in cash_flows)
    if not has_outflow:
        return 0.0
    if not has_inflow:
        return -1.0  # invested with nothing back = total loss

    def npv(rate):
        return sum(cf / ((1 + rate) ** yr) for yr, cf in cash_flows)

    def npv_deriv(rate):
        return sum(-yr * cf / ((1 + rate) ** (yr + 1)) for yr, cf in cash_flows)

    rate = 0.1
    try:
        for _ in range(200):
            f = npv(rate)
            if abs(f) < 1e-6:
                break
            df = npv_deriv(rate)
            if df == 0:
                return 0.0  # can't converge; don't report the initial guess
            rate -= f / df
            rate = max(-0.999, min(rate, 100.0))  # clamp to prevent overflow
    except (OverflowError, ZeroDivisionError):
        return 0.0

    if not (-0.999 < rate < 100.0):
        return 0.0
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
            val = (company.latest_valuation or 0)
            debt = company.debt_outstanding or 0
            net_val = max(0, val - debt)
            portfolio_value += net_val * (stake.ownership_pct / 100.0)

        if portfolio_value > 0:
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


def team_gp_income(team):
    """
    GP income earned by the firm (not the fund):
    - Management fees charged to their funds each year (on committed capital)
    - minus operating costs (fund-size-based %, accrued each year fees are charged)
    - plus carried interest on a NET basis per fund: performance fee rate x
      max(0, total realized proceeds - total invested in realized deals),
      so losses (incl. bankruptcies) offset gains.
    Returns dict with mgmt_fees, operating_costs, carried_interest, total,
    per_partner ($M).
    """
    mgmt_fees = 0.0
    operating_costs = 0.0
    carried_interest = 0.0
    ledger = []   # line items: {'year', 'kind', 'description', 'amount'} (GP view)
    exits = []    # realized exits feeding the carry basis

    for fund in team.funds:
        opex_rate = fund.operating_cost_rate or 0
        fee_txs = (FundTransaction.query
                   .filter_by(fund_id=fund.id, transaction_type='management_fee')
                   .order_by(FundTransaction.game_year)
                   .all())
        for tx in fee_txs:
            mgmt_fees += abs(tx.amount)
            ledger.append({'year': tx.game_year, 'kind': 'fee',
                           'description': f'Management fee earned — {fund.name}',
                           'amount': abs(tx.amount)})
            # Opex accrues for each year the fund operated
            opex = fund.total_capital * opex_rate
            operating_costs += opex
            ledger.append({'year': tx.game_year, 'kind': 'opex',
                           'description': f'Operating costs — {fund.name} '
                                          f'({opex_rate * 100:.2f}% of committed)',
                           'amount': -opex})

        # Net realized result across this fund's exited deals (carry basis)
        stakes = (DealEquity.query
                  .join(Deal, DealEquity.deal_id == Deal.id)
                  .filter(DealEquity.fund_id == fund.id,
                          Deal.status.in_(['liquidated', 'bankrupt']))
                  .all())
        net_realized = 0.0
        last_exit_year = None
        for stake in stakes:
            payout_txs = (FundTransaction.query
                          .filter_by(fund_id=fund.id,
                                     transaction_type='liquidation_proceeds',
                                     company_id=stake.deal.company_id)
                          .all())
            payout = sum(tx.amount for tx in payout_txs)
            net = payout - stake.equity_invested
            net_realized += net
            exit_year = payout_txs[0].game_year if payout_txs else stake.deal.game_year
            last_exit_year = max(last_exit_year or 0, exit_year)
            exits.append({'year': exit_year, 'fund': fund.name,
                          'company': stake.deal.company.name,
                          'outcome': stake.deal.status,
                          'invested': stake.equity_invested,
                          'proceeds': payout, 'net': net})
        fund_carry = max(0.0, net_realized) * (fund.performance_fee_rate or 0.20)
        if fund_carry > 0:
            carried_interest += fund_carry
            ledger.append({'year': last_exit_year, 'kind': 'carry',
                           'description': f'Carried interest — {fund.name} '
                                          f'({(fund.performance_fee_rate or 0.20) * 100:.0f}% of '
                                          f'${net_realized:,.1f}M net realized gains)',
                           'amount': fund_carry})

    ledger.sort(key=lambda x: (x['year'] or 0))
    exits.sort(key=lambda x: x['year'])
    total = mgmt_fees - operating_costs + carried_interest
    partners = team.num_partners or 1
    return {
        'mgmt_fees': mgmt_fees,
        'operating_costs': operating_costs,
        'carried_interest': carried_interest,
        'total': total,
        'per_partner': total / partners,
        'ledger': ledger,
        'exits': exits,
    }


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


DEBT_TERM_YEARS = 5  # amortization horizon for deal debt


def finalize_deal(deal: Deal, final_pre_money: float, equity_stakes_data: list,
                  rolled_equity_pct: float, debt_amount: float = 0.0,
                  debt_rate: float = 0.0, mgmt_option_pct: float = 0.0):
    """
    equity_stakes_data: [{'team_id':..., 'fund_id':..., 'equity_invested':...}, ...]

    Two deal structures:
    - VC (startup/developing/early_revenue): final_pre_money is pre-money;
      equity + debt are NEW money injected into the company;
      post-money = pre + equity + debt.
    - Buyout (mature): final_pre_money is the purchase valuation paid to the
      SELLERS; equity + debt fund the purchase, nothing is injected;
      company value stays the purchase price.
    """
    is_buyout = deal.company.stage == 'mature'
    total_equity = sum(s['equity_invested'] for s in equity_stakes_data)
    if is_buyout:
        # Equity can't exceed what the purchase needs beyond the debt
        cap = max(0.0, final_pre_money - debt_amount)
    else:
        # Cap total equity at funds wanted
        cap = deal.company.capital_requested
    if total_equity > cap:
        scale = cap / total_equity
        for s in equity_stakes_data:
            s['equity_invested'] = round(s['equity_invested'] * scale, 4)
        total_equity = cap
    post_money = (final_pre_money if is_buyout
                  else final_pre_money + total_equity + debt_amount)

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
    company.funded_valuation = post_money
    company.management_option_pct = mgmt_option_pct / 100.0
    company.debt_outstanding = debt_amount
    company.debt_interest_rate = debt_rate
    company.debt_years_remaining = DEBT_TERM_YEARS if debt_amount > 0 else 0
    if is_buyout:
        # Purchase price went to the sellers; company keeps only its own cash
        company.company_funds = company.available_cash or 0.0
    else:
        # New money lands on the balance sheet alongside existing cash
        company.company_funds = (company.available_cash or 0.0) + total_equity + debt_amount
    company.year_funded = deal.game_year

    db.session.commit()
