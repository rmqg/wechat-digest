import unittest

from agent_console.report_metrics import body_chinese_count, peak_time_text


class BodyChineseCountTests(unittest.TestCase):
    def test_displayed_body_excludes_ui_sources_and_ranking(self):
        report = {
            'edition': 'review', 'title': '不算主标题', 'chat_display': '不算群名',
            'one_line': '总览 ABC123，',
            'topics': [{'topic': '小标题', 'summary': '概述', 'details': ['要点一', '要点二'],
                        'samples': ['隐藏的样本'], 'references': [{'text': '不算来源原文'}]}],
            'quotes': [{'text': '原话', 'speaker': '不算昵称'}],
            'participants': [{'display_name': '不算活跃排行'}],
            'stats': {'label': '不算统计'}, 'range_text': '不算时间',
        }
        self.assertEqual(body_chinese_count(report), 15)
        report['edition'] = 'brief'
        self.assertEqual(body_chinese_count(report), 13)

    def test_explicit_empty_details_do_not_count_hidden_sample_fallback(self):
        report = {'topics': [{'topic': '标题', 'summary': '概述', 'details': [], 'samples': ['旧样本']}]}
        self.assertEqual(body_chinese_count(report), 4)
        del report['topics'][0]['details']
        self.assertEqual(body_chinese_count(report), 7)

    def test_han_only_including_extended_and_compatibility_characters(self):
        self.assertEqual(body_chinese_count({'one_line': '一㐀𠀀﨑\U0002ebf0\U000323b0，。ABC123かなカナ😀'}), 6)

    def test_peak_keeps_month_day_and_minutes_without_mutating_metadata(self):
        for raw, expected in [('2026-10-28 16:30', '10-28 16:30'),
                              ('2026-12-31 23:50', '12-31 23:50'),
                              ('2027-01-01 00:00', '01-01 00:00'), ('16:30', '16:30')]:
            with self.subTest(raw=raw):
                report = {'leaders': {'peak': {'name': raw}}}
                self.assertEqual(peak_time_text(report), expected)
                self.assertEqual(report['leaders']['peak']['name'], raw)


if __name__ == '__main__':
    unittest.main()
