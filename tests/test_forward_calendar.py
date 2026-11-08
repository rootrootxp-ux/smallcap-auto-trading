"""Official-calendar evidence and independently fixed experiment boundaries."""
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.forward_study import protocol


SESSIONS = ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13',
            '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20',
            '2026-11-24', '2026-11-25', '2026-11-26', '2026-11-27', '2026-11-30',
            '2026-12-01', '2026-12-02', '2026-12-03', '2026-12-04', '2026-12-07']
ORIGINAL_HASHES = {
    'frozen/core.py': '71d551b5c9f919f7919169ab41e910ecfce79d51e7c01d978e8e6a4f2d7f544f',
    'frozen/config.json': '9a41b28eab027fde689c70fb0504dd75243ab5f8a48647346f01007fdd4cbb00',
    'frozen/watchlist.csv': '5a1798e053e9a07496ae6092d5fe1e79920c15f35456f3de83c3c5543c27d12b',
}
OFFICIAL = {
    'calendar_sources/jpx-calendar.html': (
        'https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html',
        'cda2f71c21cec168b8a30db5873e5542bac53575f392a2bcd7c4aab7409523d0'),
    'calendar_sources/jpx-tse-hours.html': (
        'https://www.jpx.co.jp/equities/trading/domestic/01.html',
        '5b9ad3c96c4755b1c031e19bdde6c2c9edfe12bcb439e75215335db37949d201'),
}


class ForwardCalendarTests(unittest.TestCase):
    def test_first_twenty_sessions_match_official_holidays_and_weekends_exactly(self):
        calendar = json.loads((protocol.PACKAGE / 'calendar.json').read_text())
        start, end = date(2026, 11, 9), date(2026, 12, 7)
        body = (protocol.PACKAGE / 'calendar_sources/jpx-calendar.html').read_text()
        holidays = {date.fromisoformat(value.replace('/', '-'))
                    for value in re.findall(r'2026/\d{2}/\d{2}', body)}
        weekdays = [start + timedelta(days=n) for n in range((end - start).days + 1)
                    if (start + timedelta(days=n)).weekday() < 5]
        expected = [str(day) for day in weekdays if day not in holidays]
        self.assertEqual(len(weekdays), 21)
        self.assertEqual({str(day) for day in weekdays if day in holidays}, {'2026-11-23'})
        self.assertEqual(expected, SESSIONS)
        self.assertEqual(calendar['sessions'], SESSIONS)
        self.assertEqual(protocol.load_protocol()['sessions'], SESSIONS)
        self.assertEqual(calendar['excluded_holidays'], [{'date': '2026-11-23', 'name': '勤労感謝の日'}])

    def test_official_bodies_have_original_hashes_and_relevant_equity_market_text(self):
        calendar = json.loads((protocol.PACKAGE / 'calendar.json').read_text())
        self.assertEqual({source['path'] for source in calendar['sources']}, set(OFFICIAL))
        for source in calendar['sources']:
            url, expected = OFFICIAL[source['path']]
            body = (protocol.PACKAGE / source['path']).read_bytes()
            self.assertEqual(source['url'], url)
            self.assertEqual(source['http_status'], 200)
            self.assertEqual(source['sha256'], expected)
            self.assertEqual(hashlib.sha256(body).hexdigest(), expected)
            self.assertLess(protocol.utc(source['retrieved_at']), protocol.utc('2026-11-09T09:00:00+09:00'))
        holiday_text = (protocol.PACKAGE / 'calendar_sources/jpx-calendar.html').read_text()
        hours_text = (protocol.PACKAGE / 'calendar_sources/jpx-tse-hours.html').read_text()
        self.assertRegex(holiday_text, r'2026/11/23（[^）]+）</td><td[^>]*>勤労感謝の日')
        for text in ('土曜日', '日曜日', '国民の祝日及び休日', '12時30分から15時30分まで'):
            self.assertIn(text, hours_text)

    def test_fifty_symbols_and_pr5_engine_configuration_watchlist_remain_byte_exact(self):
        settings = protocol.load_protocol()
        codes = protocol.symbols()
        self.assertEqual(len(codes), 50)
        self.assertEqual(len(set(codes)), 50)
        self.assertFalse(set(codes) & {'4449.T', '5585.T', '5595.T'})
        self.assertIn('464A.T', codes)
        self.assertEqual(settings['expected_symbol_days'], 50 * len(SESSIONS))
        for relative, expected in ORIGINAL_HASHES.items():
            self.assertEqual(settings['frozen_files'][relative], expected)
            self.assertEqual(hashlib.sha256((protocol.PACKAGE / relative).read_bytes()).hexdigest(), expected)
        event_package = protocol.PACKAGE.parent / 'event_study'
        self.assertEqual((protocol.PACKAGE / 'frozen/core.py').read_bytes(), (event_package / 'core.py').read_bytes())
        self.assertEqual((protocol.PACKAGE / 'frozen/config.json').read_bytes(), (event_package / 'config.json').read_bytes())

    def test_schedule_and_cutoff_use_japan_time_without_extending_the_window(self):
        settings = protocol.load_protocol()
        self.assertEqual(settings['scheduled_collection_jst'], '17:15')
        self.assertEqual(settings['scheduled_evaluation_jst'], '19:00')
        for day in (SESSIONS[0], SESSIONS[-1]):
            collect = datetime.fromisoformat(day + 'T17:15:00').replace(tzinfo=ZoneInfo('Asia/Tokyo'))
            evaluate = datetime.fromisoformat(day + 'T19:00:00').replace(tzinfo=ZoneInfo('Asia/Tokyo'))
            self.assertEqual(protocol.utc(collect).strftime('%H:%M'), '08:15')
            self.assertEqual(protocol.utc(evaluate).strftime('%H:%M'), '10:00')
        self.assertEqual(settings['final_cutoff'], '2026-12-07T18:30:00+09:00')
        self.assertLess(protocol.utc(settings['registered_at']), protocol.utc(SESSIONS[0] + 'T09:00:00+09:00'))
        self.assertEqual(settings['start_date'], SESSIONS[0])
        self.assertEqual(settings['end_date'], SESSIONS[-1])

    def test_changed_frozen_code_config_or_universe_is_rejected(self):
        original_package = protocol.PACKAGE
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            shutil.copytree(original_package / 'frozen', package / 'frozen')
            shutil.copytree(original_package / 'calendar_sources', package / 'calendar_sources')
            shutil.copyfile(original_package / 'calendar.json', package / 'calendar.json')
            shutil.copyfile(protocol.PROTOCOL_PATH, package / 'protocol.json')
            with patch.object(protocol, 'PACKAGE', package), patch.object(protocol, 'PROTOCOL_PATH', package / 'protocol.json'):
                for relative in ORIGINAL_HASHES:
                    with self.subTest(file=relative):
                        path = package / relative
                        original = path.read_bytes()
                        path.write_bytes(original + b' ')
                        with self.assertRaisesRegex(ValueError, 'Frozen input changed'):
                            protocol.load_protocol()
                        path.write_bytes(original)
                self.assertEqual(protocol.load_protocol()['sessions'], SESSIONS)


if __name__ == '__main__':
    unittest.main()
