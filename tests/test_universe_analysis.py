import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import universe_analysis as analysis


def daily(periods=60, volume=200_000):
    return pd.DataFrame({'Open':1000.,'High':1010.,'Low':990.,'Close':1000.,'Volume':float(volume)},
                        index=pd.bdate_range('2026-08-03',periods=periods))


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def write(path, rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def status(code, state='検証可能'):
    return {'コード':code,'状態':state,'日足本数':60,'5分足本数':1800,
            '取得開始':'2026-09-16 09:00:00+09:00','取得終了':'2026-11-02 15:20:00+09:00',
            '検証開始':'2026-09-16 09:00:00+09:00','検証終了':'2026-11-02 15:20:00+09:00',
            '取得日数':30,'利用日数':25 if state=='検証可能' else 6,'除外日数':5 if state=='検証可能' else 24,
            '除外理由':'欠損のある日は除外','日足参照欠落日数':0,'詳細':''}


def result(code, pnl, eligible=True):
    return {'コード':code,'戦略':'MAのみ','片道スリッページ(円/株)':1.,'費用シナリオ':'仮定費用込み',
            '検証状態':'検証済み' if eligible else '利用日数不足','損益(円)':pnl,'取引数':2,
            '期間最大DD(円)':400.,'利用日数':25 if eligible else 6,
            '取得開始':'2026-09-16 09:00:00+09:00','取得終了':'2026-11-02 15:20:00+09:00',
            '開始':'2026-09-16 09:00:00+09:00','終了':'2026-11-02 15:20:00+09:00'}


class UniverseAnalysisTests(unittest.TestCase):
    def test_before_cutoff_excludes_same_day_and_future_mutations(self):
        frame=daily()
        before='2026-09-16 09:05:00+09:00'
        original=analysis.daily_metrics(frame,before=before)
        changed=frame.copy()
        changed.loc[changed.index.date>=pd.Timestamp(before).date(),['High','Low','Close','Volume']]=[50000,1,20000,9000000]
        self.assertEqual(analysis.daily_metrics(changed,before=before),original)
        self.assertEqual(original['count'],20)
        self.assertEqual(original['window_end'],'2026-09-15')
        self.assertTrue(original['qualified'])

    def test_fixed_thresholds_and_missing_rows_are_not_backfilled(self):
        frame=daily(periods=20,volume=100000)
        metrics=analysis.daily_metrics(frame)
        self.assertTrue(metrics['qualified'])  # Exactly JPY100m and 2% qualify.
        frame.iloc[-1,frame.columns.get_loc('Close')]=float('nan')
        metrics=analysis.daily_metrics(frame)
        self.assertEqual(metrics['count'],19)
        self.assertFalse(metrics['qualified'])
        more=daily(periods=21)
        more.iloc[-1,more.columns.get_loc('Volume')]=float('nan')
        self.assertEqual(analysis.daily_metrics(more)['count'],19)
        self.assertFalse(analysis.daily_metrics(daily(periods=20,volume=99999))['qualified'])
        self.assertFalse(analysis.daily_metrics(daily(periods=19))['qualified'])

    def test_unavailable_metrics_remain_unknown(self):
        metrics=analysis.daily_metrics(pd.DataFrame())
        self.assertEqual(metrics['count'],0)
        self.assertIsNone(metrics['avg_value_yen'])
        self.assertIsNone(metrics['avg_range_pct'])
        self.assertFalse(metrics['qualified'])

    def fixtures(self, data):
        write(data/'legacy_watchlist20.csv',[{'コード':'A','候補名':'old A'},{'コード':'B','候補名':'old B'}])
        write(data/'baseline20_summary.csv',[result('A',300),result('B',400)])
        write(data/'baseline20_status.csv',[status('A'),status('B')])
        items=[{'コード':code,'候補名':code,'selection_reason':'流動性と値幅の事前規則',
                'selected_at':'2026-11-03 08:00:00+09:00','selection_info_end':'2026-10-23',
                'listing_asof':'2026-11-03','market_cap_observed_at':'2026-11-03',
                'market_cap_yen':50_000_000_000} for code in ('A','C')]
        return items

    def test_audit_and_cohorts_keep_composition_quality_and_account_caveats(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);data=base/'data';data.mkdir();out=base/'out'
            items=self.fixtures(data)
            with patch.object(analysis,'DATA_DIR',data):
                fragment=analysis.write_analysis(out,'run-id',items,{'A':daily(),'C':daily(volume=1)},
                    [status('A'),status('C','データ不足')],[result('A',100),result('C',10000,eligible=False)])
            audit=read(out/'universe_audit.csv')
            self.assertEqual(len(audit),2)
            self.assertEqual({r['実行ID'] for r in audit},{'run-id'})
            self.assertTrue(all(r['selection_reason']=='流動性と値幅の事前規則' for r in audit))
            bias=read(out/'bias_audit.csv')
            self.assertTrue(all(r['選定日足に検証開始日以降を含む']=='True' for r in bias))
            self.assertTrue(all(r['選定後の未参照期間評価']=='未検証（選定後の取得データなし）' for r in bias))
            rows=read(out/'cohort_comparison.csv')
            self.assertEqual(sum(r['record_type']=='cohort_summary' for r in rows),72)
            self.assertTrue(any(r['比較群']=='LEGACY20_BASELINE_41539585377' for r in rows))
            group=next(r for r in rows if r['record_type']=='cohort_summary' and r['比較群']=='CURRENT_UNIVERSE_CURRENT_RUN' and r['戦略']=='MAのみ' and r['片道スリッページ(円/株)']=='1.0' and r['費用シナリオ']=='仮定費用込み')
            self.assertEqual((group['検証済み行数'],group['参考値行数']),('1','1'))
            self.assertEqual(float(group['検証済み平均損益(円)']),100)
            self.assertEqual(float(group['検証済み独立条件損益合算(円)']),100)
            legacy=next(r for r in rows if r['比較群']=='LEGACY20_ON_CURRENT_DATA' and r['戦略']=='MAのみ' and r['片道スリッページ(円/株)']=='1.0' and r['費用シナリオ']=='仮定費用込み')
            self.assertEqual(legacy['今回対象外数'],'1')
            self.assertEqual(legacy['結果行なし銘柄数'],'1')
            paired=next(r for r in rows if r['record_type']=='paired_symbol')
            self.assertEqual(paired['コード'],'A')
            self.assertEqual(paired['比較期間一致'],'True')
            self.assertEqual(float(paired['今回引く旧run損益差(円)']),-200)
            self.assertIn('価格の同一性は未確認',paired['比較制約'])
            self.assertIn('全銘柄共通の100万円口座',fragment)
            self.assertIn('現在時価総額',fragment)
            self.assertIn('合計損益を比較して改善とは判断できません',fragment)

    def test_paired_period_mismatch_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);data=base/'data';data.mkdir();out=base/'out'
            items=self.fixtures(data)
            older=result('A',300)
            older['取得開始']='2026-09-17 09:00:00+09:00'
            write(data/'baseline20_summary.csv',[older])
            with patch.object(analysis,'DATA_DIR',data):
                analysis.write_analysis(out,'run-id',items,{'A':daily()},[status('A'),status('C','取得失敗')],[result('A',100)])
            paired=next(r for r in read(out/'cohort_comparison.csv') if r['record_type']=='paired_symbol')
            self.assertEqual(paired['取得期間一致'],'False')
            self.assertEqual(paired['品質後期間一致'],'True')
            self.assertEqual(paired['比較期間一致'],'False')
            self.assertIn('期間差',paired['比較制約'])

    def test_duplicate_condition_does_not_silently_overwrite_prior_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);data=base/'data';data.mkdir()
            items=self.fixtures(data)
            with patch.object(analysis,'DATA_DIR',data),self.assertRaisesRegex(ValueError,'duplicate cohort'):
                analysis.write_analysis(base/'out','id',items,{'A':daily()},[status('A')],[result('A',1),result('A',2)])
