"""Game Dynamics — admin-tunable simulation constants.

The simulation's assumptions live as module-level constants in ``models`` and
``game_logic`` (and a couple in ``app``). This module exposes a curated set of
them as adjustable parameters:

  * DEFAULTS live in code (the ``default`` field below).
  * Only changed values are persisted, in the ``game_setting`` table.
  * ``apply_overrides()`` writes the persisted values onto the live module
    globals at startup and after every save, so future cranks/rolls pick them
    up. Past results are never rewritten.

Each parameter carries ``get``/``set`` callables so dict entries (e.g.
MANAGEMENT_RETURN_TILT['strong']), tuple bounds (VOL_FACTOR_RANGE) and
constants duplicated across modules (DEBT_INTEREST_RATE) are all handled
uniformly. ``app`` is imported lazily inside the callables to avoid a circular
import (app imports this module at load time).
"""

import models
import game_logic


def _app():
    import app
    return app


def _set_tilt(key, v):
    game_logic.MANAGEMENT_RETURN_TILT[key] = v


def _set_runway(key, v):
    models.BURN_RUNWAY_YEARS[key] = v


def _set_recovery_prob(key, v):
    game_logic.VENTURE_RECOVERY_PROB[key] = v


def _set_vol_range(index, v):
    lo, hi = game_logic.VOL_FACTOR_RANGE
    pair = [lo, hi]
    pair[index] = v
    game_logic.VOL_FACTOR_RANGE = (pair[0], pair[1])


def _set_debt_rate(v):
    # DEBT_INTEREST_RATE is held by both game_logic and app (imported by name).
    game_logic.DEBT_INTEREST_RATE = v
    setattr(_app(), 'DEBT_INTEREST_RATE', v)


# unit: 'percent' -> stored as a fraction, shown x100 with a % suffix.
#       'number'  -> stored and shown as-is, with the given suffix.
# min/max/step are in DISPLAY units.
PARAMS = [
    # ---- Returns & Risk -------------------------------------------------
    dict(key='mgmt_strong', group='Returns & Risk',
         label='Strong management tilt', unit='percent', suffix='%',
         min=-20, max=20, step=0.5, default=0.02,
         help="Expected-return boost for a company with strong management.",
         get=lambda: game_logic.MANAGEMENT_RETURN_TILT['strong'],
         set=lambda v: _set_tilt('strong', v)),
    dict(key='mgmt_weak', group='Returns & Risk',
         label='Weak management tilt', unit='percent', suffix='%',
         min=-20, max=20, step=0.5, default=-0.03,
         help="Expected-return drag for weak management (negative).",
         get=lambda: game_logic.MANAGEMENT_RETURN_TILT['weak'],
         set=lambda v: _set_tilt('weak', v)),
    dict(key='growth_weight', group='Returns & Risk',
         label='Revenue-growth return weight', unit='number', suffix='',
         min=0, max=1, step=0.01, default=0.10,
         help="Weight on above-typical revenue growth when tilting expected "
              "return (0.10 = 10 pts of excess growth → +1%).",
         get=lambda: game_logic.GROWTH_RETURN_WEIGHT,
         set=lambda v: setattr(game_logic, 'GROWTH_RETURN_WEIGHT', v)),
    dict(key='margin_weight', group='Returns & Risk',
         label='EBITDA-margin return weight', unit='number', suffix='',
         min=0, max=1, step=0.01, default=0.20,
         help="Weight on above-typical EBITDA margin when tilting expected return.",
         get=lambda: game_logic.MARGIN_RETURN_WEIGHT,
         set=lambda v: setattr(game_logic, 'MARGIN_RETURN_WEIGHT', v)),
    dict(key='burn_weight', group='Returns & Risk',
         label='Cash-burn return weight', unit='number', suffix='',
         min=0, max=1, step=0.01, default=0.15,
         help="Weight on burn/value vs. the stage norm when tilting expected "
              "return. Burning more than typical for the value drags return; "
              "burning less lifts it (0.15 = 10 pts of below-typical burn → +1.5%).",
         get=lambda: game_logic.BURN_RETURN_WEIGHT,
         set=lambda v: setattr(game_logic, 'BURN_RETURN_WEIGHT', v)),
    dict(key='max_fund_tilt', group='Returns & Risk',
         label='Max fundamentals tilt', unit='percent', suffix='%',
         min=0, max=25, step=0.5, default=0.05,
         help="Cap on the combined fundamentals tilt to expected return (±).",
         get=lambda: game_logic.MAX_FUNDAMENTALS_TILT,
         set=lambda v: setattr(game_logic, 'MAX_FUNDAMENTALS_TILT', v)),
    dict(key='margin_vol_weight', group='Returns & Risk',
         label='Margin → volatility weight', unit='number', suffix='',
         min=0, max=3, step=0.1, default=0.6,
         help="How strongly EBITDA margin moves a holding's volatility.",
         get=lambda: game_logic.MARGIN_VOL_WEIGHT,
         set=lambda v: setattr(game_logic, 'MARGIN_VOL_WEIGHT', v)),
    dict(key='vol_factor_min', group='Returns & Risk',
         label='Volatility multiplier floor', unit='number', suffix='×',
         min=0.1, max=1, step=0.05, default=0.75,
         help="Lower bound on the volatility multiplier.",
         get=lambda: game_logic.VOL_FACTOR_RANGE[0],
         set=lambda v: _set_vol_range(0, v)),
    dict(key='vol_factor_max', group='Returns & Risk',
         label='Volatility multiplier cap', unit='number', suffix='×',
         min=1, max=3, step=0.05, default=1.25,
         help="Upper bound on the volatility multiplier.",
         get=lambda: game_logic.VOL_FACTOR_RANGE[1],
         set=lambda v: _set_vol_range(1, v)),
    dict(key='distress_penalty', group='Returns & Risk',
         label='Distress return penalty', unit='percent', suffix='%',
         min=0, max=30, step=0.5, default=0.05,
         help="Permanent expected-return penalty once a company has run out of "
              "cash even once.",
         get=lambda: game_logic.DISTRESS_RETURN_PENALTY,
         set=lambda v: setattr(game_logic, 'DISTRESS_RETURN_PENALTY', v)),
    dict(key='generalist_return', group='Returns & Risk',
         label='Generalist return factor', unit='number', suffix='×',
         min=0.5, max=1, step=0.01, default=0.95,
         help="Generalist funds' expected return as a multiple of the sector mean.",
         get=lambda: game_logic.GENERALIST_RETURN_FACTOR,
         set=lambda v: setattr(game_logic, 'GENERALIST_RETURN_FACTOR', v)),
    dict(key='generalist_vol', group='Returns & Risk',
         label='Generalist volatility factor', unit='number', suffix='×',
         min=0.5, max=1.5, step=0.01, default=0.90,
         help="Generalist funds' volatility as a multiple of the sector mean.",
         get=lambda: game_logic.GENERALIST_VOL_FACTOR,
         set=lambda v: setattr(game_logic, 'GENERALIST_VOL_FACTOR', v)),

    # ---- Cash & Burn ----------------------------------------------------
    dict(key='ebitda_cash_yield', group='Cash & Burn',
         label='EBITDA → cash yield', unit='percent', suffix='%',
         min=0, max=200, step=1, default=0.60,
         help="Share of positive EBITDA that becomes spendable cash each year "
              "(mature companies).",
         get=lambda: models.EBITDA_CASH_YIELD,
         set=lambda v: setattr(models, 'EBITDA_CASH_YIELD', v)),
    dict(key='ebitda_burn_multiple', group='Cash & Burn',
         label='Negative-EBITDA burn multiple', unit='number', suffix='×',
         min=0, max=3, step=0.05, default=1.25,
         help="Cash drained per $1 of negative EBITDA.",
         get=lambda: models.EBITDA_BURN_MULTIPLE,
         set=lambda v: setattr(models, 'EBITDA_BURN_MULTIPLE', v)),
    dict(key='runway_startup', group='Cash & Burn',
         label='Runway target — startup', unit='number', suffix='yrs',
         min=0.5, max=10, step=0.5, default=2.5,
         help="Years of runway a fully funded startup's raise should buy.",
         get=lambda: models.BURN_RUNWAY_YEARS['startup'],
         set=lambda v: _set_runway('startup', v)),
    dict(key='runway_developing', group='Cash & Burn',
         label='Runway target — developing', unit='number', suffix='yrs',
         min=0.5, max=10, step=0.5, default=3.0,
         help="Years of runway a fully funded developing company's raise should buy.",
         get=lambda: models.BURN_RUNWAY_YEARS['developing'],
         set=lambda v: _set_runway('developing', v)),
    dict(key='runway_early_revenue', group='Cash & Burn',
         label='Runway target — early revenue', unit='number', suffix='yrs',
         min=0.5, max=10, step=0.5, default=4.0,
         help="Years of runway a fully funded early-revenue company's raise should buy.",
         get=lambda: models.BURN_RUNWAY_YEARS['early_revenue'],
         set=lambda v: _set_runway('early_revenue', v)),
    dict(key='burn_evolution_rate', group='Cash & Burn',
         label='Burn evolution rate', unit='number', suffix='',
         min=0, max=2, step=0.05, default=0.5,
         help="Fraction of the year's return by which burn moves inversely "
              "(0.5 = half-rate).",
         get=lambda: game_logic.BURN_EVOLUTION_RATE,
         set=lambda v: setattr(game_logic, 'BURN_EVOLUTION_RATE', v)),

    # ---- Debt -----------------------------------------------------------
    dict(key='debt_interest_rate', group='Debt',
         label='Debt interest rate', unit='percent', suffix='%',
         min=0, max=30, step=0.25, default=0.08,
         help="Annual interest rate on all deal debt (interest-only).",
         get=lambda: game_logic.DEBT_INTEREST_RATE,
         set=_set_debt_rate),
    dict(key='debt_term_years', group='Debt',
         label='Debt term', unit='number', suffix='yrs',
         min=1, max=15, step=1, default=7,
         help="Stated loan term shown to teams (debt is interest-only; "
              "principal is repaid at exit).",
         get=lambda: game_logic.DEBT_TERM_YEARS,
         set=lambda v: setattr(game_logic, 'DEBT_TERM_YEARS', v)),
    dict(key='max_debt_pct', group='Debt',
         label='Max debt on a buyout', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.60,
         help="Maximum debt as a share of the purchase valuation on a buyout.",
         get=lambda: _app().MAX_DEBT_PCT,
         set=lambda v: setattr(_app(), 'MAX_DEBT_PCT', v)),

    # ---- Player Actions -------------------------------------------------
    dict(key='change_mgmt_cost', group='Player Actions',
         label='Change-management cost', unit='number', suffix='$M',
         min=0, max=100, step=0.5, default=5.0,
         help="Flat fee a fund pays to replace a portfolio company's "
              "management team.",
         get=lambda: _app().CHANGE_MGMT_COST,
         set=lambda v: setattr(_app(), 'CHANGE_MGMT_COST', v)),
    dict(key='max_dividend_pct', group='Player Actions',
         label='Max dividend per payout', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.20,
         help="The most a lead can dividend out of a portfolio company's cash "
              "in a single payout (as a share of the company's cash on hand).",
         get=lambda: _app().MAX_DIVIDEND_PCT,
         set=lambda v: setattr(_app(), 'MAX_DIVIDEND_PCT', v)),
    dict(key='change_mgmt_p_weak', group='Player Actions',
         label='New management: chance of Weak', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.25,
         help="Probability a replaced management team turns out weak.",
         get=lambda: _app().CHANGE_MGMT_P_WEAK,
         set=lambda v: setattr(_app(), 'CHANGE_MGMT_P_WEAK', v)),
    dict(key='change_mgmt_p_average', group='Player Actions',
         label='New management: chance of Average', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.50,
         help="Probability a replaced management team turns out average.",
         get=lambda: _app().CHANGE_MGMT_P_AVERAGE,
         set=lambda v: setattr(_app(), 'CHANGE_MGMT_P_AVERAGE', v)),
    dict(key='change_mgmt_p_strong', group='Player Actions',
         label='New management: chance of Strong', unit='percent', suffix='%',
         derived=True,
         help="Calculated as 100% minus weak and average, so the three "
              "always add up to 100%.",
         get=lambda: max(0.0, 1.0 - _app().CHANGE_MGMT_P_WEAK
                         - _app().CHANGE_MGMT_P_AVERAGE)),

    # ---- Venture Recovery -----------------------------------------------
    dict(key='recovery_p_startup', group='Venture Recovery',
         label='Turn-profitable chance — startup', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.20,
         help="Chance each year that a cash-exhausted startup turns profitable "
              "instead of going bankrupt.",
         get=lambda: game_logic.VENTURE_RECOVERY_PROB['startup'],
         set=lambda v: _set_recovery_prob('startup', v)),
    dict(key='recovery_p_developing', group='Venture Recovery',
         label='Turn-profitable chance — developing', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.50,
         help="Chance each year that a cash-exhausted developing company turns "
              "profitable instead of going bankrupt.",
         get=lambda: game_logic.VENTURE_RECOVERY_PROB['developing'],
         set=lambda v: _set_recovery_prob('developing', v)),
    dict(key='recovery_p_early_revenue', group='Venture Recovery',
         label='Turn-profitable chance — early revenue', unit='percent', suffix='%',
         min=0, max=100, step=5, default=0.80,
         help="Chance each year that a cash-exhausted early-revenue company turns "
              "profitable instead of going bankrupt.",
         get=lambda: game_logic.VENTURE_RECOVERY_PROB['early_revenue'],
         set=lambda v: _set_recovery_prob('early_revenue', v)),
    dict(key='venture_max_profit_margin', group='Venture Recovery',
         label='Max margin when turning profitable', unit='percent', suffix='%',
         min=0, max=100, step=1, default=0.20,
         help="When a venture company turns profitable, its EBITDA margin is "
              "drawn at random between 0 and this cap (applied to projected revenue).",
         get=lambda: game_logic.VENTURE_MAX_PROFIT_MARGIN,
         set=lambda v: setattr(game_logic, 'VENTURE_MAX_PROFIT_MARGIN', v)),
    dict(key='startup_revenue_seed_factor', group='Venture Recovery',
         label='Pre-revenue startup seed', unit='percent', suffix='%',
         min=0, max=300, step=5, default=0.50,
         help="Seed revenue for a pre-revenue startup, as a share of the capital "
              "it raised (used to project revenue).",
         get=lambda: models.STARTUP_REVENUE_SEED_FACTOR,
         set=lambda v: setattr(models, 'STARTUP_REVENUE_SEED_FACTOR', v)),
    dict(key='revenue_growth_decay', group='Venture Recovery',
         label='Revenue growth decay', unit='number', suffix='×',
         min=0, max=1, step=0.05, default=0.70,
         help="Each year, a venture company's revenue growth rate is this multiple "
              "of the prior year's (taper, so high early growth doesn't run away).",
         get=lambda: models.REVENUE_GROWTH_DECAY,
         set=lambda v: setattr(models, 'REVENUE_GROWTH_DECAY', v)),
]

GROUPS = ['Returns & Risk', 'Cash & Burn', 'Debt', 'Player Actions', 'Venture Recovery']

_BY_KEY = {p['key']: p for p in PARAMS}


def to_display(param, internal):
    """Internal (stored) value -> display value."""
    return internal * 100.0 if param['unit'] == 'percent' else internal


def to_internal(param, display):
    """Display value -> internal (stored) value."""
    return display / 100.0 if param['unit'] == 'percent' else display


def apply_overrides():
    """Load persisted overrides from the DB onto the live module globals.

    Safe to call repeatedly. Must run inside an app context.
    """
    from models import GameSetting
    rows = GameSetting.query.all()
    for row in rows:
        param = _BY_KEY.get(row.key)
        if param and not param.get('derived'):
            param['set'](row.value)


def current_view():
    """Build the template view: one dict per parameter, grouped."""
    view = {g: [] for g in GROUPS}
    for p in PARAMS:
        internal = p['get']()
        entry = {
            'key': p['key'],
            'label': p['label'],
            'help': p['help'],
            'suffix': p['suffix'],
            'unit': p['unit'],
            'value': round(to_display(p, internal), 4),
            'derived': p.get('derived', False),
        }
        if not p.get('derived'):
            entry.update({
                'default': round(to_display(p, p['default']), 4),
                'min': p['min'],
                'max': p['max'],
                'step': p['step'],
                'is_default': abs(internal - p['default']) < 1e-9,
            })
        view[p['group']].append(entry)
    return view


def save_from_form(form):
    """Parse posted display values, persist + apply. Returns (ok, errors, n)."""
    from models import db, GameSetting
    errors = []
    changed = 0
    for p in PARAMS:
        if p.get('derived'):
            continue
        raw = form.get(p['key'])
        if raw is None or raw == '':
            continue
        try:
            disp = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{p['label']}: '{raw}' is not a number.")
            continue
        # Clamp to the parameter's allowed display range.
        disp = max(p['min'], min(p['max'], disp))
        internal = to_internal(p, disp)

        is_default = abs(internal - p['default']) < 1e-9
        row = GameSetting.query.get(p['key'])
        if is_default:
            # Don't persist defaults — keep the table to genuine overrides.
            if row:
                db.session.delete(row)
                changed += 1
        else:
            if row:
                if abs(row.value - internal) > 1e-12:
                    row.value = internal
                    changed += 1
            else:
                db.session.add(GameSetting(key=p['key'], value=internal))
                changed += 1
        # Apply to the live globals regardless.
        p['set'](internal)

    if not errors:
        db.session.commit()
    else:
        db.session.rollback()
    return (not errors, errors, changed)


def reset_all():
    """Delete all overrides and restore code defaults on the live globals."""
    from models import db, GameSetting
    GameSetting.query.delete()
    db.session.commit()
    for p in PARAMS:
        if not p.get('derived'):
            p['set'](p['default'])


def reset_one(key):
    """Delete one param's override and restore its code default on the live
    globals. Returns the param's label (or None if the key is unknown)."""
    from models import db, GameSetting
    p = _BY_KEY.get(key)
    if not p or p.get('derived'):
        return None
    row = GameSetting.query.get(key)
    if row:
        db.session.delete(row)
        db.session.commit()
    p['set'](p['default'])
    return p['label']
