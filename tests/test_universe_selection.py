import csv
import hashlib
import json
from pathlib import Path
import unittest
from scripts.select_universe import eligible, select, POLICY

ROOT=Path(__file__).resolve().parents[1]


class UniverseSelectionTests(unittest.TestCase):
    def row(self, code, value=1e9):
        return {'コード':code,'listed':True,'currency':'JPY','market_cap_yen':5e10,
                'count':20,'avg_value_yen':value,'avg_range_pct':3.0}

    def test_objective_size_liquidity_and_range_gates(self):
        row=self.row('TEST')
        self.assertTrue(eligible(row))
        for field,value in [('listed',False),('market_cap_yen',4e9),('market_cap_yen',1.1e12),
                            ('avg_value_yen',99e6),('avg_range_pct',1.99),('count',19),('currency','USD')]:
            with self.subTest(field=field,value=value):
                self.assertFalse(eligible(dict(row,**{field:value})))

    def test_rank_does_not_consume_pnl_or_intraday_quality(self):
        pool=[self.row(f'{i:04}.T',1e9+i) for i in range(60)]
        first=[r['コード'] for r in select(pool)]
        for i,row in enumerate(pool):
            row.update(PnL=1e9 if i<10 else -1e9,intraday_quality_days=0 if i>=10 else 30)
        self.assertEqual(first,[r['コード'] for r in select(pool)])
        self.assertEqual(first[0],'0059.T')
        with self.assertRaisesRegex(ValueError,'do not relax'):
            select(pool[:49])

    def test_ties_are_deterministic(self):
        pool=[self.row(f'{i:04}.T') for i in reversed(range(51))]
        self.assertEqual([r['コード'] for r in select(pool)],[f'{i:04}.T' for i in range(50)])

    def test_committed_50_have_provenance_and_exclude_retired_symbols(self):
        file=ROOT/'watchlist.csv'
        with file.open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
        policy=json.loads((ROOT/'data/universe50/selection_policy.json').read_text())
        self.assertEqual(len(rows),50)
        self.assertEqual(len({r['コード'] for r in rows}),50)
        self.assertFalse({'4449.T','5585.T','5595.T'} & {r['コード'] for r in rows})
        self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),policy['selected_sha256'])
        for row in rows:
            self.assertTrue(row['selection_reason'])
            self.assertTrue(row['selected_at'])
            self.assertLessEqual(float(row['market_cap_yen']),1e12)
            self.assertGreaterEqual(float(row['market_cap_yen']),5e9)
            self.assertGreaterEqual(float(row['avg_value_yen']),1e8)
            self.assertGreaterEqual(float(row['avg_range_pct']),2.)
        self.assertEqual(POLICY['size'],50)
