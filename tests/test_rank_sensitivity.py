import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pandas as pd

import rank_sensitivity as ranking


def daily(past_volume, current_volume):
    frame=pd.DataFrame({'Open':1000.,'High':1010.,'Low':990.,'Close':1000.,'Volume':float(past_volume)},
                       index=pd.bdate_range('2026-08-03',periods=60))
    frame.loc[frame.index.date>=pd.Timestamp('2026-09-16').date(),'Volume']=float(current_volume)
    return frame


def pool():
    return [{'コード':code,'候補名':code,'listed':True,'listing_asof':'20260930',
             'market_cap_yen':50e9,'currency':'JPY','market_cap_observed_at':'2026-11-03'}
            for code in ('A','B','C')]


def frames():
    return {'A':daily(200000,200000),'B':daily(150000,500000),'C':daily(300000,100000)}


def write(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RankSensitivityTests(unittest.TestCase):
    def test_future_changes_cannot_change_past_ranking_but_change_current_rank(self):
        source=frames()
        before=ranking.compute_rank_rows(pool(),source,{'A','B'},'2026-09-16',size=2)
        changed={code:frame.copy() for code,frame in source.items()}
        changed['C'].loc[changed['C'].index.date>=pd.Timestamp('2026-09-16').date(),'Volume']=1000000.
        after=ranking.compute_rank_rows(pool(),changed,{'A','B'},'2026-09-16',size=2)
        self.assertEqual({r['コード']:r['先行順位'] for r in before},{r['コード']:r['先行順位'] for r in after})
        self.assertNotEqual({r['コード']:r['現在順位'] for r in before},{r['コード']:r['現在順位'] for r in after})
        self.assertTrue(all(r['先行指標終了']=='2026-09-15' for r in before))
        self.assertEqual({r['コード']:r['区分'] for r in before},{'A':'common','B':'current_only','C':'past_only'})

    def test_ties_are_deterministic_and_invalid_fixed_market_metadata_excludes(self):
        stocks=pool()
        source={code:daily(200000,200000) for code in ('A','B','C')}
        stocks[2]['listed']=False
        rows=ranking.compute_rank_rows(stocks,source,{'A'},'2026-09-16',size=1)
        index={r['コード']:r for r in rows}
        self.assertEqual(index['A']['先行順位'],1)
        self.assertEqual(index['B']['先行順位'],2)
        self.assertIsNone(index['C']['先行順位'])
        self.assertFalse(index['C']['先行適格'])

    def fixture(self,root):
        data=root/'data/universe50';data.mkdir(parents=True)
        stocks=pool();source=frames()
        with zipfile.ZipFile(data/'selection_inputs.zip','w') as archive:
            for row in stocks:
                code=row['コード'];raw=source[code].to_csv().encode()
                row['daily_sha256']=sha(raw)
                archive.writestr(code+'.csv',raw)
                archive.writestr(code+'.json',json.dumps({key:row[key] for key in
                    ['daily_sha256','market_cap_yen','currency','market_cap_observed_at']}))
        write(data/'candidate_pool.csv',[{'コード':r['コード'],'候補名':r['候補名']} for r in stocks])
        write(data/'pool_audit.csv',stocks)
        write(root/'watchlist.csv',[{'コード':'B','候補名':'B'},{'コード':'A','候補名':'A'}])
        (data/'jpx_listing_20260930.xlsx').write_bytes(b'fixed listing fixture')
        policy={'min_market_cap_yen':5e9,'max_market_cap_yen':1e12,'daily_window':20,
                'min_avg_value_yen':1e8,'min_avg_range_pct':2.,'size':2,'pool_size':3,'qualified_size':3,
                'pool_sha256':sha((data/'candidate_pool.csv').read_bytes()),
                'selection_inputs_sha256':sha((data/'selection_inputs.zip').read_bytes()),
                'selected_sha256':sha((root/'watchlist.csv').read_bytes()),
                'listing_sha256':sha((data/'jpx_listing_20260930.xlsx').read_bytes())}
        (data/'selection_policy.json').write_text(json.dumps(policy))
        return data,policy

    def test_snapshot_verification_exports_all_rows_ids_and_caveats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            with patch.object(ranking,'ROOT',root):
                fragment=ranking.write_rank_sensitivity(root/'output','run-id','2026-09-16')
            with (root/'output/selection_rank_audit.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),3)
            self.assertEqual({r['実行ID'] for r in rows},{'run-id'})
            self.assertTrue(all(r['現在順位再現検査']=='一致' and r['入力ハッシュ検査']=='一致' for r in rows))
            self.assertIn('共通1銘柄',fragment)
            self.assertIn('現在の手動母集団',fragment)
            self.assertIn('先行側のみの銘柄の5分足成績は未検証',fragment)

    def test_changed_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data,_=self.fixture(root)
            with (data/'selection_inputs.zip').open('ab') as f:f.write(b'changed')
            with patch.object(ranking,'ROOT',root),self.assertRaisesRegex(ValueError,'hash mismatch'):
                ranking.write_rank_sensitivity(root/'output','id','2026-09-16')

    def test_self_consistent_watchlist_hash_cannot_hide_rank_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data,policy=self.fixture(root)
            write(root/'watchlist.csv',[{'コード':'C','候補名':'C'},{'コード':'A','候補名':'A'}])
            policy['selected_sha256']=sha((root/'watchlist.csv').read_bytes())
            (data/'selection_policy.json').write_text(json.dumps(policy))
            with patch.object(ranking,'ROOT',root),self.assertRaisesRegex(ValueError,'current rank'):
                ranking.write_rank_sensitivity(root/'output','id','2026-09-16')
