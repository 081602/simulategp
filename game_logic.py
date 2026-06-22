"""
Core game logic: crank algorithms, IRR calculation, lead selection.
"""
import random
import math
from datetime import datetime
from models import (db, Game, Team, Fund, GameCompany, CompanySearch,
                    TermSheet, Deal, DealEquity, FundTransaction, Notification,
                    ReturnAssumption, ebitda_to_cash)


# Shown on the management-fee line (Fund Economics + GP Economics) when a fund
# owes a fee but lacks the cash to pay it in full.
MGMT_FEE_SHORTFALL_NOTE = "Insufficient capital at the fund level to pay management fee"


# ---------------------------------------------------------------------------
# Lead Selection Algorithm (Phase 1 Crank)
# ---------------------------------------------------------------------------

def _lead_loss_reason(company, losing_ts, winning_ts, winning_team):
    """Spell out the concrete term-sheet differences that cost a team the lead."""
    edges = []
    if winning_ts.pre_money_valuation > losing_ts.pre_money_valuation:
        edges.append(f"a higher valuation (${winning_ts.pre_money_valuation:,.1f}M "
                     f"vs your ${losing_ts.pre_money_valuation:,.1f}M)")
    if winning_ts.liquidation_preference < losing_ts.liquidation_preference:
        edges.append(f"a lighter liquidation preference "
                     f"({winning_ts.liquidation_preference}x vs your "
                     f"{losing_ts.liquidation_preference}x)")
    if losing_ts.participation and not winning_ts.participation:
        edges.append("no participation rights (yours demanded participation)")
    ad_rank = {'none': 0, 'weighted': 1, 'full_ratchet': 2}
    if ad_rank.get(winning_ts.anti_dilution, 0) < ad_rank.get(losing_ts.anti_dilution, 0):
        edges.append(f"lighter anti-dilution terms ({winning_ts.anti_dilution} "
                     f"vs your {losing_ts.anti_dilution})")
    cap = company.capital_requested or 0
    if (cap > 0 and losing_ts.total_investment < cap
            and winning_ts.total_investment > losing_ts.total_investment):
        edges.append(f"more of the funding need covered "
                     f"(${winning_ts.total_investment:,.1f}M vs your "
                     f"${losing_ts.total_investment:,.1f}M of ${cap:,.1f}M sought)")

    if edges:
        return (f"{company.name} chose {winning_team.firm_name}'s term sheet, "
                f"which offered {'; '.join(edges)}.")
    return (f"{company.name} chose {winning_team.firm_name}'s term sheet — "
            f"the offers were close, but theirs scored marginally better overall.")


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
                reason = _lead_loss_reason(company, ts, lead_ts, lead_team)
                ts.rejection_reason = (
                    f"{reason} You did not offer to participate as a fill "
                    f"investor, so your term sheet was rejected.")
                _notify(ts.team_id,
                        f"Your term sheet on {company.name} was not selected as "
                        f"lead. {reason}",
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

def _ask_anchor(company: GameCompany, deal: Deal) -> float:
    """Base that a holding's FIRST-year mark grows from: the company's original
    ask, restated to be comparable to funded_valuation.

    Rewards entry discipline. Because the first roll grows off the ask (not what
    the team paid), buying below the ask makes the year-1 mark land above cost
    (capturing more than the expected return) while overpaying makes it land at
    or below cost (capturing less). Paying exactly the ask captures the return.

    Buyouts: initial_val_ask is the whole-company ask, directly comparable to the
    purchase price. VC: initial_val_ask is a PRE-money number, so add the team's
    equity (and any debt) to get the ask-implied post-money — the apples-to-apples
    counterpart of funded_valuation (= pre + equity + debt).
    """
    ask = company.initial_val_ask
    if ask is None or ask <= 0:
        # No ask on record — fall back to what was paid (original behavior).
        return company.latest_valuation or 10.0
    if company.stage == 'mature':
        return ask
    return ask + (deal.total_equity_invested or 0.0) + (deal.debt_amount or 0.0)


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

    # 0. Force-close any deal still awaiting co-investor responses: outstanding
    #    offers auto-decline and the lead backstops them, so the year can advance
    pending_coinvest = (
        Deal.query
        .join(GameCompany, Deal.company_id == GameCompany.id)
        .filter(GameCompany.game_id == game.id,
                Deal.status == 'pending_coinvest')
        .all()
    )
    for deal in pending_coinvest:
        close_deal_with_coinvestors(deal)

    # 1. Management fees — charged on INVESTED (deployed) capital, i.e. the
    #    capital currently at work in active holdings, NOT committed capital.
    #    Charged before this year's deals roll/exit, so it reflects the capital
    #    deployed entering the year. A fund with nothing deployed pays no fee.
    for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
        for fund in team.funds:
            if not fund.is_active:
                continue
            fee = fund.deployed_capital * fund.management_fee_rate
            # No deployed capital ⇒ no fee owed ⇒ no line at all.
            if fee <= 1e-9:
                continue
            # Cap the fee at the cash the fund actually has — it can't pay more
            # than it holds, and the ledger should match the cash that moved.
            # (GPs can pull cash up via portfolio dividends to cover their fees.)
            paid = min(fee, max(0.0, fund.available_capital))
            # Always post the fee line so the year is visible even when the fund
            # can't pay — show what was actually paid ($0 or partial) and flag the
            # shortfall for transparency.
            desc = f"Year {year} management fee"
            if fee - paid > 1e-9:
                desc += (f" — {MGMT_FEE_SHORTFALL_NOTE} "
                         f"(${fee:,.1f}M owed, ${paid:,.1f}M paid)")
            fund.available_capital -= paid
            # Avoid negative zero so the ledger shows a clean "$0.0M".
            _record_transaction(fund.id, 'management_fee', (-paid) or 0.0, desc, year)

    # 2. Simulate company performance for all active deals
    active_deals = (
        Deal.query
        .join(GameCompany, Deal.company_id == GameCompany.id)
        .filter(GameCompany.game_id == game.id, Deal.status == 'active')
        .all()
    )

    for deal in active_deals:
        company = deal.company
        # Whether a previously-distressed venture failed its recovery roll this
        # year (used to flag "rescued but still burning" if it survives).
        recovery_failed = False

        # Roll outcome. The FIRST year after funding grows off the company's
        # original ask (see _ask_anchor) so entry price discipline drives the
        # year-1 return; every year thereafter compounds off the current mark.
        multiple = _roll_outcome(company, game.market_condition)
        if year == company.year_funded:
            base_val = _ask_anchor(company, deal)
        else:
            base_val = company.latest_valuation or 10.0
        new_val = base_val * multiple
        company.set_year_val(year, max(0.0, new_val))
        # Record the pure market roll so the holding page can show the market
        # move separately from cash events (follow-ons, fees, dividends).
        company.set_year_return(year, multiple)

        # Cash events charged THIS year move the mark dollar-for-dollar — the
        # valuation incorporates the company's cash. They're applied to this
        # year's mark (the year they were charged, after the market roll) so the
        # holding-page valuation bridge ties out exactly:
        #   prior mark x roll  +  follow-on  -  mgmt fees  -  dividends  =  mark.
        cash_event = (company.get_year_followon(year)
                      - company.get_year_mgmt_fee(year)
                      - company.get_year_dividend(year))
        if cash_event:
            company.set_year_val(year, max(0.0, company.get_year_val(year) + cash_event))

        # Cash flow into the balance:
        #  - MATURE: EBITDA moves with the year's return (sign-aware) and is
        #    converted to cash (60% of a profit). Profits grow with the company.
        #  - VENTURE: no EBITDA-to-cash; the company burns cash. The burn moves
        #    INVERSELY with the return at half the rate (return +20% -> burn -10%,
        #    return -20% -> burn +10%), floored at $0. Then it burns that amount.
        annual_return = multiple - 1.0
        if company.stage == 'mature' or company.turned_profitable:
            # Mature buyouts — and venture companies that have turned profitable —
            # generate cash: EBITDA moves with the return and converts to cash.
            if company.ltm_ebitda is not None:
                company.ltm_ebitda = company.ltm_ebitda + annual_return * abs(company.ltm_ebitda)
                company.set_year_ebitda(year, company.ltm_ebitda)
                company.company_funds += ebitda_to_cash(company.ltm_ebitda)
        else:
            # Venture, still pre-profit. Track projected revenue each year.
            rev = company.projected_revenue(year)
            company.set_year_revenue(year, rev)
            if company.ever_distressed:
                # In recovery: a stage-based chance of turning profitable.
                p = VENTURE_RECOVERY_PROB.get(company.stage, 0.0)
                if random.random() < p:
                    margin = random.uniform(0.0, VENTURE_MAX_PROFIT_MARGIN)
                    company.ltm_revenue = rev
                    company.ltm_ebitda = rev * margin
                    company.ltm_ebitda_margin = margin
                    company.turned_profitable = True
                    company.annual_burn_rate = 0.0
                    company.set_year_ebitda(year, company.ltm_ebitda)
                    company.company_funds += ebitda_to_cash(company.ltm_ebitda)
                    company.in_distress = False
                    company.distress_resolution = 'recovered'
                    company.distress_resolution_year = year
                    for stake in deal.equity_stakes:
                        _notify(stake.team_id,
                                f"{company.name} turned profitable — it now generates "
                                f"cash and is no longer at risk of running dry.",
                                'crank_complete', company.id)
                else:
                    recovery_failed = True
                    company.annual_burn_rate = max(
                        0.0, (company.annual_burn_rate or 0.0) * (1 - BURN_EVOLUTION_RATE * annual_return))
                    company.set_year_burn(year, company.annual_burn_rate)
                    company.company_funds -= company.annual_burn_rate
            else:
                company.annual_burn_rate = max(
                    0.0, (company.annual_burn_rate or 0.0) * (1 - BURN_EVOLUTION_RATE * annual_return))
                company.set_year_burn(year, company.annual_burn_rate)
                company.company_funds -= company.annual_burn_rate

        # Debt service: INTEREST ONLY (bullet loan). The principal does not
        # amortize — it stays outstanding for the life of the hold and is repaid
        # from sale proceeds at exit (the liquidation waterfall pays debt first).
        if company.debt_outstanding > 0:
            interest = company.debt_outstanding * company.debt_interest_rate
            company.company_funds -= interest

        # Valuation wipeout -> immediate bankruptcy (market forces, not cash)
        if (company.latest_valuation or 0) <= 0:
            _process_bankruptcy(deal, company, year, reason='wipeout')
            continue

        # Cash exhausted handling.
        if company.company_funds < 0:
            recoverable_venture = (company.stage != 'mature'
                                   and not company.turned_profitable)
            if recoverable_venture and company.ever_distressed:
                # Already in recovery; this year's profitability roll failed and
                # there was no cash left to fund the burn -> bankrupt.
                _process_bankruptcy(deal, company, year)
                continue
            if not recoverable_venture and company.in_distress:
                # Mature (or turned-profitable venture) two-strike: bankrupt.
                _process_bankruptcy(deal, company, year)
                continue
            company.in_distress = True
            company.ever_distressed = True   # permanent scar: tilts future returns down
            company.company_funds = 0.0
            deal.let_it_roll = False          # fresh decision for this distress episode
            if recoverable_venture:
                msg = (f"{company.name} has run out of cash. There's a chance it "
                       f"turns profitable next year — but if it doesn't and you "
                       f"haven't injected cash, it goes bankrupt. Inject enough "
                       f"cash to keep it alive while it tries to turn the corner.")
            else:
                msg = (f"{company.name} is in financial distress — cash exhausted. "
                       f"Without action it will go bankrupt next year.")
            for stake in deal.equity_stakes:
                _notify(stake.team_id, msg, 'distress', company.id)
        else:
            company.in_distress = False
            # Was distressed and rescued (follow-on) but the recovery roll failed
            # — it survived the year on the injected cash, yet is still burning.
            # Tell the team the rescue didn't turn the corner this year.
            if recovery_failed:
                company.distress_resolution = 'still_burning'
                company.distress_resolution_year = year
                for stake in deal.equity_stakes:
                    _notify(stake.team_id,
                            f"{company.name} received a cash injection but did not "
                            f"turn profitable — it is still burning cash.",
                            'distress', company.id)

        # Liquidation check
        if deal.marked_for_liquidation:
            _process_liquidation(deal, company, year)

    # 3. End of the fund's term: after the final year's performance has rolled,
    #    exit every remaining holding and close the game out.
    if year >= (game.total_years or 7):
        final_deals = (
            Deal.query
            .join(GameCompany, Deal.company_id == GameCompany.id)
            .filter(GameCompany.game_id == game.id, Deal.status == 'active')
            .all()
        )
        for deal in final_deals:
            _process_liquidation(deal, deal.company, year, force=True)
        game.current_phase = 2
        game.status = 'completed'
        db.session.commit()
        for team in Team.query.filter_by(game_id=game.id, is_admin=False).all():
            _notify(team.id,
                    f"The fund's term has ended after Year {year}. All remaining "
                    f"holdings were exited at their final valuations — see your "
                    f"final results.",
                    'crank_complete')
        return

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
# Cash-burn tilt: venture companies are EXPECTED to burn, so what matters is burn
# RELATIVE TO VALUE vs. the stage norm. Burning much more than typical (cash-
# inefficient) drags expected return; burning less than typical lifts it. Ratios
# below are annual burn / company value (calibrated to each stage's median).
STAGE_TYPICAL_BURN_RATIO = {'startup': 0.20, 'developing': 0.15, 'early_revenue': 0.09}
BURN_RETURN_WEIGHT = 0.15     # 10 pts of below-typical burn/value -> +1.5% expected return
MAX_FUNDAMENTALS_TILT = 0.05  # cap on total expected-return shift (+/- 5%)
MARGIN_VOL_WEIGHT = 0.6       # 10 pts of above-typical margin -> -6% relative volatility
VOL_FACTOR_RANGE = (0.75, 1.25)
# Management quality tilts expected return (weak destroys more than strong adds)
MANAGEMENT_RETURN_TILT = {'strong': 0.02, 'average': 0.0, 'weak': -0.03}
# A company that has run out of cash even once carries a permanent scar: its
# expected annual return is tilted down by this much from then on.
DISTRESS_RETURN_PENALTY = 0.05
# A generalist fund earns slightly less than a sector-focused fund: its
# expected return is 5% lower and its volatility 10% lower than the sector
# mean (both relative — multiply the base assumption).
GENERALIST_RETURN_FACTOR = 0.95
GENERALIST_VOL_FACTOR = 0.90
# Venture burn evolves inversely with the year's return at this fraction of the
# rate (0.5 = half-rate: a +20% return cuts burn 10%, a -20% return raises it 10%).
BURN_EVOLUTION_RATE = 0.5
# When a non-mature company runs out of cash it gets a stage-based chance, each
# year, of turning profitable instead of going bankrupt. Teams are told a chance
# exists but never the number. On success, EBITDA = projected revenue x a random
# margin in (0, VENTURE_MAX_PROFIT_MARGIN].
VENTURE_RECOVERY_PROB = {'startup': 0.20, 'developing': 0.50, 'early_revenue': 0.80}
VENTURE_MAX_PROFIT_MARGIN = 0.20


def _fundamentals_adjustment(company: GameCompany, mu: float, sigma: float):
    """Tilt (mu, sigma) by how the company's growth/margin/burn compare to stage-typical values.

    Above-typical revenue growth or EBITDA margin raises expected return;
    above-typical margin also dampens volatility (steadier businesses), and
    below-typical margin amplifies it. Cash burn is judged RELATIVE TO VALUE
    against the stage norm — venture companies are expected to burn, so only a
    burn/value ratio well above typical drags return (cash-inefficient), while a
    below-typical ratio lifts it. Revenue growth and margin only apply when the
    company has revenue (both are undefined otherwise); companies without
    metrics are unaffected.
    """
    typical_growth, typical_margin = STAGE_TYPICAL_FUNDAMENTALS.get(
        company.stage, (0.20, 0.10))

    # Revenue growth and EBITDA margin are both undefined without revenue, so
    # they only tilt companies that actually have revenue.
    has_revenue = bool(company.ltm_revenue)
    has_margin = company.ltm_ebitda_margin is not None and has_revenue

    tilt = 0.0
    if company.revenue_growth_3yr is not None and has_revenue:
        tilt += GROWTH_RETURN_WEIGHT * (company.revenue_growth_3yr - typical_growth)
    if has_margin:
        tilt += MARGIN_RETURN_WEIGHT * (company.ltm_ebitda_margin - typical_margin)
    # Cash-burn tilt: compare burn/value to the stage norm. High burn for the
    # value (ratio above typical) tilts down; low burn (below typical) tilts up.
    typical_burn = STAGE_TYPICAL_BURN_RATIO.get(company.stage)
    value = company.latest_valuation or 0.0
    if typical_burn and company.annual_burn_rate and value > 0:
        burn_ratio = company.annual_burn_rate / value
        tilt += BURN_RETURN_WEIGHT * (typical_burn - burn_ratio)
    tilt = max(-MAX_FUNDAMENTALS_TILT, min(MAX_FUNDAMENTALS_TILT, tilt))

    vol_factor = 1.0
    if has_margin:
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
    # Generalist lead funds see a sector mean 5% lower at 10% lower volatility
    # than sector specialists (applied to the base before company-specific
    # tilts). The mandate already keeps a focused fund in its own sector, so
    # the lead team's focus settles every case.
    lead = company.lead_team
    if lead and lead.sector_focus == 'generalist':
        mu *= GENERALIST_RETURN_FACTOR
        sigma *= GENERALIST_VOL_FACTOR
    mu, sigma = _fundamentals_adjustment(company, mu, sigma)
    mu += MANAGEMENT_RETURN_TILT.get(company.management_quality, 0.0)
    # Permanent scar: a MATURE company that has ever run out of cash returns less.
    # Non-mature companies are expected to run dry at some point (it's the nature
    # of venture), so no scar applies to them.
    if company.ever_distressed and company.stage == 'mature':
        mu -= DISTRESS_RETURN_PENALTY
    annual_return = random.gauss(mu, sigma)
    multiple = max(0.0, 1.0 + annual_return) * market_condition
    return multiple


def _process_bankruptcy(deal: Deal, company: GameCompany, year: int,
                        reason: str = 'distress'):
    company.status = 'bankrupt'
    deal.status = 'bankrupt'
    # Record the outcome for the last-period recap. A valuation wipeout (the
    # market roll took the mark to $0) is "market forces"; a cash-exhaustion
    # bankruptcy only counts for holdings that went through the distress cycle.
    if reason == 'wipeout':
        company.distress_resolution = 'wipeout'
        company.distress_resolution_year = year
        msg = (f"Market forces caused {company.name} to go bankrupt — "
               f"its valuation was wiped out and the investment is lost.")
    else:
        if company.ever_distressed:
            company.distress_resolution = 'bankrupt'
            company.distress_resolution_year = year
        msg = f"{company.name} has gone bankrupt. Your investment has been lost."
    for stake in deal.equity_stakes:
        _notify(stake.team_id, msg, 'liquidation', company.id)
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
    investor_ownership = sum(s.ownership_pct for s in deal.equity_stakes)
    as_converted = distributable * (investor_ownership / 100.0)

    if deal.company.stage == 'mature':
        # Buyouts hold common equity — no liquidation preference. Proceeds are
        # split straight pro-rata to ownership.
        pref_multiple = None
        liq_pref_amount = None
        investor_payout = as_converted
        method = 'pro_rata'
    else:
        pref_multiple = deal.liquidation_preference or 1
        liq_pref_amount = invested * pref_multiple
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


def _process_liquidation(deal: Deal, company: GameCompany, year: int, force: bool = False):
    """Distribute proceeds according to liquidation waterfall.

    force=True (used at game end) sells regardless of the reserve price.
    """
    proceeds = company.latest_valuation or 0
    reserve = deal.reserve_price or 0

    if not force and proceeds < reserve:
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

    # 2. Allocate to equity. Buyouts (common equity) get straight pro-rata;
    #    only non-mature preferred equity carries a liquidation preference.
    investor_ownership = sum(s.ownership_pct for s in deal.equity_stakes)

    if company.stage == 'mature':
        investor_payout = remaining * (investor_ownership / 100.0)
    else:
        total_invested = deal.total_equity_invested
        liq_pref_amount = total_invested * deal.liquidation_preference
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
    IRR via Newton-Raphson with a bisection fallback.
    cash_flows: list of (year, amount) tuples; negative = outflow, positive = inflow.
    Returns IRR as a decimal (e.g., 0.25 = 25%).

    Newton is fast but can fail to converge on some cash-flow shapes (it would
    otherwise return 0.0 and make a losing fund look break-even). When it
    doesn't converge, fall back to bisection, which reliably finds the root
    whenever one is bracketed in (-99.99%, 10000%).
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

    # 1) Newton-Raphson
    rate = 0.1
    try:
        for _ in range(200):
            f = npv(rate)
            if abs(f) < 1e-6:
                break
            df = npv_deriv(rate)
            if df == 0:
                break
            rate = max(-0.9999, min(rate - f / df, 100.0))
        if abs(npv(rate)) < 1e-4 and -0.9999 < rate < 100.0:
            return round(rate, 4)
    except (OverflowError, ZeroDivisionError):
        pass

    # 2) Bisection fallback over a wide bracket
    lo, hi = -0.9999, 100.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return 0.0
    if f_lo == 0:
        return round(lo, 4)
    if f_hi == 0:
        return round(hi, 4)
    if f_lo * f_hi > 0:
        return 0.0  # no sign change -> root not bracketed
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return round(mid, 4)
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round((lo + hi) / 2, 4)


def team_gp_income(team):
    """
    GP income earned by the firm (not the fund):
    - Management fees charged to their funds each year (on invested/deployed capital)
    - minus operating costs (fund-size-based %, accrued each year the fund operates)
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
    carry_funds = []  # per-fund carry calculation breakdown

    # Last year that has actually been cranked. The crank advances current_year
    # after every year except the final one (the game completes in place without
    # advancing), so the cut-off depends on game status.
    game = Game.query.get(team.game_id)
    if game and game.status == 'completed':
        last_cranked_year = game.current_year
    elif game:
        last_cranked_year = game.current_year - 1
    else:
        last_cranked_year = 0

    for fund in team.funds:
        opex_rate = fund.operating_cost_rate or 0
        fee_txs = (FundTransaction.query
                   .filter_by(fund_id=fund.id, transaction_type='management_fee')
                   .order_by(FundTransaction.game_year)
                   .all())
        fund_mgmt_fees = 0.0
        for tx in fee_txs:
            mgmt_fees += abs(tx.amount)
            fund_mgmt_fees += abs(tx.amount)
            fee_desc = f'Management fee earned — {fund.name}'
            # Surface a fund-level shortfall in the GP ledger too.
            if tx.description and MGMT_FEE_SHORTFALL_NOTE.lower() in tx.description.lower():
                fee_desc = f'{fund.name} — {tx.description}'
            ledger.append({'year': tx.game_year, 'kind': 'fee',
                           'description': fee_desc,
                           'amount': abs(tx.amount)})

        # Operating costs are the GP's running expenses — incurred every year the
        # fund operates (vintage through the last cranked year), independent of
        # whether a management fee was charged or paid that year.
        if opex_rate:
            for op_year in range(fund.year_raised, last_cranked_year + 1):
                opex = fund.total_capital * opex_rate
                operating_costs += opex
                ledger.append({'year': op_year, 'kind': 'opex',
                               'description': f'Operating costs — {fund.name} '
                                              f'({opex_rate * 100:.2f}% of committed)',
                               'amount': -opex})

        # Net realized result across this fund's exited deals (carry basis)
        stakes = (DealEquity.query
                  .join(Deal, DealEquity.deal_id == Deal.id)
                  .filter(DealEquity.fund_id == fund.id,
                          Deal.status.in_(['liquidated', 'bankrupt']))
                  .all())
        rate = fund.performance_fee_rate or 0.20
        net_realized = 0.0
        last_exit_year = None
        fund_exits = []
        total_invested = 0.0
        total_proceeds = 0.0
        for stake in stakes:
            payout_txs = (FundTransaction.query
                          .filter_by(fund_id=fund.id,
                                     transaction_type='liquidation_proceeds',
                                     company_id=stake.deal.company_id)
                          .all())
            payout = sum(tx.amount for tx in payout_txs)
            net = payout - stake.equity_invested
            net_realized += net
            total_invested += stake.equity_invested
            total_proceeds += payout
            exit_year = payout_txs[0].game_year if payout_txs else stake.deal.game_year
            last_exit_year = max(last_exit_year or 0, exit_year)
            row = {'year': exit_year, 'fund': fund.name,
                   'company': stake.deal.company.name,
                   'outcome': stake.deal.status,
                   'invested': stake.equity_invested,
                   'proceeds': payout, 'net': net}
            exits.append(row)
            fund_exits.append(row)
        # Carry is NET of management fees: fees the LPs paid reduce the profit
        # the GP takes carry on (net basis, not gross basis)
        carry_basis = net_realized - fund_mgmt_fees
        fund_carry = max(0.0, carry_basis) * rate
        if fund_carry > 0:
            carried_interest += fund_carry
            companies_str = ', '.join(e['company'] for e in fund_exits)
            ledger.append({'year': last_exit_year, 'kind': 'carry',
                           'description': f'Carried interest — {fund.name} '
                                          f'on {companies_str} '
                                          f'({rate * 100:.0f}% of '
                                          f'${carry_basis:,.1f}M net of mgmt fees)',
                           'amount': fund_carry})
        if fund_exits:
            carry_funds.append({
                'fund': fund.name,
                'rate': rate,
                'exits': fund_exits,
                'total_invested': total_invested,
                'total_proceeds': total_proceeds,
                'net_realized': net_realized,
                'mgmt_fees': fund_mgmt_fees,
                'carry_basis': carry_basis,
                'carry': fund_carry,
            })

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
        'carry_funds': carry_funds,
    }


def team_simple_return(team, game):
    """A simple, student-friendly fund return: net proceeds vs. committed capital,
    annualized over the fund's life.

      net value = available cash + unrealized holdings - carry owed to the GP
                  (management fees are already deducted from available cash)
      multiple  = net value / committed capital
      annualized = multiple ** (1 / years) - 1

    Returns every component so the calculation can be shown on screen.
    """
    funds = [f for f in team.funds if f.is_active]
    committed = sum(f.total_capital for f in funds)
    available = sum(f.available_capital for f in funds)

    active_stakes = (DealEquity.query
                     .join(Deal, DealEquity.deal_id == Deal.id)
                     .filter(DealEquity.team_id == team.id, Deal.status == 'active')
                     .all())
    unrealized = sum(s.current_value for s in active_stakes)

    gp = team_gp_income(team)
    carry = gp['carried_interest']
    mgmt_fees = gp['mgmt_fees']

    net_value = available + unrealized - carry
    years = max(1, game.current_year)
    multiple = (net_value / committed) if committed > 0 else 0.0
    if net_value > 0 and committed > 0:
        annualized = multiple ** (1.0 / years) - 1.0
    else:
        annualized = -1.0  # lost everything (or worse)

    # MOIC — multiple of *invested* capital (deal performance, before fund
    # fees/carry): total value out of the deals / total equity put into them.
    invested = sum(s.equity_invested
                   for s in DealEquity.query.filter_by(team_id=team.id).all())
    realized = sum(tx.amount for tx in (
        FundTransaction.query.join(Fund, FundTransaction.fund_id == Fund.id)
        .filter(Fund.team_id == team.id,
                FundTransaction.transaction_type.in_(
                    ['liquidation_proceeds', 'dividend_received']))
        .all()))
    total_value = realized + unrealized
    moic = (total_value / invested) if invested > 0 else 0.0

    # Deal IRR — time-weighted return on capital actually DEPLOYED into deals
    # (deal cash flows only, before fund fees/carry): the IRR twin of MOIC,
    # and the deployed-capital counterpart to the committed-capital return.
    deal_flows = {}
    for tx in (FundTransaction.query.join(Fund, FundTransaction.fund_id == Fund.id)
               .filter(Fund.team_id == team.id,
                       FundTransaction.transaction_type.in_(
                           ['investment', 'liquidation_proceeds', 'dividend_received']))
               .all()):
        deal_flows[tx.game_year] = deal_flows.get(tx.game_year, 0) + tx.amount
    if unrealized > 0:  # value remaining holdings as a terminal inflow
        deal_flows[game.current_year] = deal_flows.get(game.current_year, 0) + unrealized
    deal_cashflows = []   # year-by-year net deal cash flow, for showing the IRR
    if deal_flows:
        cf = sorted(deal_flows.items())
        base_year = cf[0][0]
        deal_irr = calculate_irr([(yr - base_year, amt) for yr, amt in cf])
        deal_cashflows = [{'year': yr, 'offset': yr - base_year, 'amount': amt}
                          for yr, amt in cf]
    else:
        deal_irr = 0.0

    return {
        'committed': committed,
        'available': available,
        'unrealized': unrealized,
        'mgmt_fees': mgmt_fees,
        'carry': carry,
        'net_value': net_value,
        'multiple': multiple,
        'years': years,
        'annualized': annualized,
        'total_return': multiple - 1.0,
        'invested': invested,
        'realized': realized,
        'total_value': total_value,
        'moic': moic,
        'deal_irr': deal_irr,
        'deal_cashflows': deal_cashflows,
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


# Deal debt is interest-only (no amortization); principal is repaid from sale
# proceeds at exit. The term spans the full 7-year fund life so there is no
# refinancing. (DEBT_TERM_YEARS only populates the legacy debt_years_remaining
# column now; it is not used to amortize.)
DEBT_TERM_YEARS = 7
DEBT_INTEREST_RATE = 0.08   # fixed market rate applied to all deal debt


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

    if not is_buyout:
        # Primary round: founders roll over ALL their equity — no secondary
        # sale is possible. Investors only ever buy newly issued shares, so
        # their ownership is purely their cash as a share of post-money and
        # founders keep the rest (their pre-money stake, diluted by the new
        # money). Rollover is therefore NOT a negotiable term for venture deals.
        rolled_equity_pct = (final_pre_money / post_money) if post_money > 0 else 0.0

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

    # The management option pool is carved out of the rolled (pre-money / seller)
    # side first, so it does NOT dilute new investors — an investor's stake is
    # worth their full investment at close. Only the excess, when the rolled side
    # is smaller than the pool, falls through and dilutes the buyer (possible on a
    # low-/no-rollover buyout; venture founders always roll the full pre-money,
    # which dwarfs the pool, so it never reaches them).
    pool_from_investors = max(0.0, mgmt_option_pct - rolled_equity_pct * 100.0)

    for s in equity_stakes_data:
        share = (s['equity_invested'] / total_equity) if total_equity > 0 else 0
        ownership = share * (investor_pool_pct - pool_from_investors)

        stake = DealEquity(
            deal_id=deal.id,
            team_id=s['team_id'],
            fund_id=s['fund_id'],
            equity_invested=s['equity_invested'],
            ownership_pct=round(ownership, 4),
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
        # Cash-free close: sellers take the balance-sheet cash along with the
        # price; the new owners start at $0 and live off the company's earnings
        company.company_funds = 0.0
        company.available_cash = 0.0
    else:
        # Venture: the company started with $0 cash and is raising it in the
        # round, so the money invested IS its post-deal cash balance.
        company.company_funds = total_equity + debt_amount
        company.available_cash = 0.0
    company.year_funded = deal.game_year

    db.session.commit()


def process_followon(deal: Deal, amount: float):
    """The lead injects fresh cash into a distressed company at the CURRENT
    valuation (a bridge/follow-on round). New money buys equity at the existing
    equity value (valuation - debt), diluting every existing holder pro-rata;
    the lead's stake grows by its purchase. The cash extends runway, the
    enterprise value rises by the cash injected, and distress clears.
    """
    company = deal.company
    debt = company.debt_outstanding or 0.0
    pre_eq = max(0.0, (company.latest_valuation or 0.0) - debt)   # pre-money equity value
    post_eq = pre_eq + amount
    if post_eq <= 0 or amount <= 0:
        return
    d = pre_eq / post_eq   # dilution factor for existing holders

    lead_stake = next((s for s in deal.equity_stakes
                       if s.team_id == deal.lead_team_id), None)
    if lead_stake is None:
        return

    # Dilute every existing holder: investor stakes, founders (rolled), and the
    # management option pool all shrink by pre/post.
    for s in deal.equity_stakes:
        s.ownership_pct = round(s.ownership_pct * d, 4)
    deal.rolled_equity_pct = (deal.rolled_equity_pct or 0.0) * d
    company.management_option_pct = (company.management_option_pct or 0.0) * d

    # The lead's new money buys (amount / post-money equity) of the company.
    lead_stake.ownership_pct = round(lead_stake.ownership_pct + (amount / post_eq) * 100.0, 4)
    lead_stake.equity_invested += amount

    # Cash lands on the balance sheet and the immediate crisis is resolved. The
    # enterprise value rises by the injected cash too, but — like the mgmt-change
    # fee and dividends — that mark bump is applied to THIS year's mark at the
    # next Deal & Return Process (the crank), so the valuation history bridge ties
    # out: prior mark x roll + follow-on - fees - dividends = this year's mark.
    company.company_funds += amount
    # Record the injection in the current year so the holding-page cash register
    # and the crank itemize it (it lands after the distressed year's cash was
    # clamped to $0).
    game = Game.query.get(company.game_id)
    company.add_year_followon(game.current_year if game else company.year_funded, amount)
    company.in_distress = False

    fund = Fund.query.get(lead_stake.fund_id)
    fund.available_capital -= amount
    _record_transaction(fund.id, 'investment', -amount,
                        f"Follow-on investment in {company.name}",
                        deal.game_year, company.id)
    _notify(deal.lead_team_id,
            f"You injected ${amount:,.1f}M into {company.name} at its current "
            f"valuation. Its runway is extended and it's no longer in distress.",
            'deal_won', company.id)
    db.session.commit()


def locked_deal_economics(deal: Deal):
    """Re-derive the binding economics (pre-money, rolled %, debt, options) from
    the accepted lead term sheet. Deterministic, so any close path agrees."""
    company = deal.company
    lead_ts = deal.lead_term_sheet
    final_pre_money = lead_ts.pre_money_valuation
    mgmt_options = (company.management_option_pct or 0.0) * 100
    if company.stage == 'mature':
        # Buyout: sellers cash out 100% (no rollover). The management option pool
        # is the only non-buyer equity and plays the rollover role in financing,
        # so the buyer's stake is worth their full equity check:
        #   price = equity + debt + management's piece, management = pool x (price - debt)
        #   => debt = price - equity / (1 - pool)
        rolled_pct = 0.0
        pool = company.management_option_pct or 0.0
        # Debt is the buyer's choice (no capacity cap). It's recovered here from
        # the stored equity check: equity = (1 - pool)(price - debt).
        implied = (final_pre_money - lead_ts.total_investment / (1 - pool)
                   if pool < 1 else final_pre_money)
        debt_amount = max(0.0, implied)
    else:
        # Venture: founders roll the full pre-money; the pool comes off their side.
        rolled_pct = lead_ts.rolled_equity_min
        debt_amount = 0.0
    debt_rate = DEBT_INTEREST_RATE if debt_amount > 0 else 0.0
    return final_pre_money, rolled_pct, debt_amount, debt_rate, mgmt_options


def close_deal_with_coinvestors(deal: Deal):
    """Close a deal that was awaiting co-investor responses.

    Any co-invest offer still outstanding (coinvest_offered) is auto-declined
    so a slow responder can't block the close; the lead's fund backstops every
    slice that isn't an accepted co-investor. Safe to call once the lead has
    proposed offers (deal.status == 'pending_coinvest').
    """
    company = deal.company
    lead_ts = deal.lead_term_sheet
    final_pre_money, rolled_pct, debt_amount, debt_rate, mgmt_options = \
        locked_deal_economics(deal)
    total_equity = lead_ts.total_investment

    # Force-decline any offer that never got a response
    outstanding = (TermSheet.query
                   .filter_by(company_id=company.id, game_year=deal.game_year,
                              status='coinvest_offered')
                   .filter(TermSheet.team_id != deal.lead_team_id)
                   .all())
    for fts in outstanding:
        fts.status = 'fill_declined'
        fts.proposed_coinvest_amount = None
        fts.rejection_reason = (
            f"The co-investment window on {company.name} closed before you "
            f"responded.")
        _notify(fts.team_id,
                f"The co-investment window on {company.name} closed before you "
                f"responded, so you were not included in the deal.",
                'deal_lost', company.id)

    # Accepted co-investors fund their agreed slice; lead backstops the rest
    accepted = (TermSheet.query
                .filter_by(company_id=company.id, game_year=deal.game_year,
                           status='fill_accepted')
                .filter(TermSheet.team_id != deal.lead_team_id)
                .all())
    fill_stakes = []
    for fts in accepted:
        amt = fts.proposed_coinvest_amount or 0.0
        if amt > 0:
            fill_stakes.append((fts, amt))
    fill_total = min(sum(a for _, a in fill_stakes), total_equity)

    stakes = [{'team_id': deal.lead_team_id, 'fund_id': lead_ts.fund_id,
               'equity_invested': total_equity - fill_total}]
    for fts, amt in fill_stakes:
        stakes.append({'team_id': fts.team_id, 'fund_id': fts.fund_id,
                       'equity_invested': amt})

    finalize_deal(deal, final_pre_money, stakes, rolled_pct,
                  debt_amount, debt_rate, mgmt_options)

    # finalize_deal committed; layer on notifications + lead reputation
    for fts, amt in fill_stakes:
        _notify(fts.team_id,
                f"The deal on {company.name} has closed. Your co-investment of "
                f"${amt:,.1f}M is now active.",
                'deal_won', company.id)
    lead = Team.query.get(deal.lead_team_id)
    if lead:
        lead.reputation = min(5.0, lead.reputation + 0.2)
    _notify(deal.lead_team_id,
            f"Your deal on {company.name} has closed"
            + (f" with {len(fill_stakes)} co-investor(s)." if fill_stakes
               else "."),
            'deal_won', company.id)
    db.session.commit()
