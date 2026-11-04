import csv
from pathlib import Path
import tempfile
import unittest
import pandas as pd

from portfolio_engine import prepare,simulate_portfolio,STRATEGIES
from portfolio_report import write_report
from scripts.check_portfolio import validate
from smallcap_auto_v3 import Costs
from test_portfolio_engine import prices


class PortfolioArtifactTests(unittest.TestCase):
    def generate(self,root,missing=False):
        frame=prices()
        if missing:frame=frame.drop(frame.index[9])
        frames={'A':frame};prepared=prepare(frames);results=[]
        for strategy in STRATEGIES:
            for slip in (1.,2.):
                for cost in [Costs('費用ゼロ'),Costs('現物手数料込み',100,0,0)]:
                    result=simulate_portfolio(frames,strategy,slip,cost,prepared=prepared)
                    label={'scenario':f'{strategy}/{slip}/{cost.name}','strategy':strategy,'slippage':slip,
                           'cost_scenario':cost.name,'fee_yen':cost.fee_yen,'fee_bps':cost.fee_bps,'annual_interest_pct':0}
                    for key in ['summary','trades','orders','equity','daily','positions']:
                        for row in [result[key]] if key=='summary' else result[key]:row.update(label)
                    results.append(result)
        write_report(root,results,[{'コード':'A','状態':'synthetic'}],[],{'note':'<script>bad</script>'})
        return results

    def test_report_and_csv_ledger_can_be_independently_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.generate(root)
            validate(root)
            text=(root/'report.html').read_text()
            self.assertIn('信用取引・空売り',text)
            self.assertIn('先読み',text)
            self.assertIn('&lt;script&gt;bad&lt;/script&gt;',text)
            self.assertNotIn('<script>bad',text)
            with (root/'portfolio_positions.csv').open(encoding='utf-8-sig') as f:
                self.assertFalse(list(csv.DictReader(f)))

    def test_unpriced_holdings_are_not_exported_as_zero_profit_or_full_dd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.generate(root,missing=True)
            with (root/'portfolio_summary.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            self.assertTrue(all(row['total_pnl']=='' and row['max_drawdown']=='' for row in rows))
            with self.assertRaisesRegex(ValueError,'All scenarios are unpriced'):
                validate(root)

    def test_validator_detects_corrupted_cash_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.generate(root)
            file=root/'portfolio_equity.csv'
            with file.open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            rows[0]['cash']=str(float(rows[0]['cash'])+1)
            with file.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            with self.assertRaisesRegex(ValueError,'cash reservation'):
                validate(root)
