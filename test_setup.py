import os, sys, traceback

sys.path.insert(0, os.path.dirname(__file__))
from app import app, db, _BASE_DIR, Game, GameCompany, Team, Fund
import json
from sqlalchemy import text

game_name = 'Test Game'
total_years = 7
qp = 10
default_capital = 100.0
admin_pw = 'admin123'

with app.test_request_context('/admin/setup', method='POST'):
    try:
        db.session.remove()
        db.engine.dispose()

        # Same approach as the view: expunge_all + rollback + raw SQL deletes + ORM inserts
        db.session.expunge_all()
        db.session.rollback()
        print(f'Session state after expunge/rollback: dirty={len(db.session.dirty)} new={len(db.session.new)}', flush=True)

        for tbl in ['deal_equity','deal','company_search','term_sheet','fund_transaction','notification','fund','game_company','team','game']:
            db.session.execute(text(f'DELETE FROM {tbl}'))
            print(f'Deleted from {tbl}', flush=True)
        db.session.flush()

        companies_path = os.path.join(_BASE_DIR, 'data', 'companies.json')
        with open(companies_path) as f:
            company_data = json.load(f)

        game = Game(name=game_name, total_years=total_years, query_points_per_year=qp)
        db.session.add(game)
        db.session.flush()
        print(f'Game inserted id={game.id}', flush=True)

        admin = Team(game_id=game.id, username='admin', firm_name='Administrator', is_admin=True, query_points=999, reputation=5.0)
        admin.set_password(admin_pw)
        db.session.add(admin)

        for cd in company_data:
            gc = GameCompany(
                game_id=game.id, name=cd['name'], sector=cd['sector'], stage=cd['stage'],
                description=cd['description'], capital_requested=cd['capital_requested'],
                rolled_equity_min=cd['rolled_equity_min'], rolled_equity_max=cd['rolled_equity_max'],
                debt_capacity=cd['debt_capacity'], is_cash_flow_positive=cd['is_cash_flow_positive'],
                dividend_eligible=cd.get('dividend_eligible', False),
                management_quality=cd['management_quality'],
                outcome_distributions=json.dumps(cd['outcome_distributions']),
                initial_val_ask=cd['base_valuation'],
                year_available=cd.get('year_available', 1),
                reasons_for_funding=cd.get('reasons_for_funding'),
                available_cash=cd.get('available_cash', 0.0),
                founder_shares=cd.get('founder_shares', 10000000),
                management_option_pct=cd.get('management_option_pct', 0.10),
            )
            db.session.add(gc)
        print('Companies added to session', flush=True)

        db.session.commit()
        print('SUCCESS - committed!', flush=True)

    except Exception as e:
        print('ERROR:', e, flush=True)
        traceback.print_exc()
