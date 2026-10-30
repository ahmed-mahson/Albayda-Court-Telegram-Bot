import csv
import os
import re
import sys
import tempfile
import unicodedata
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import case_records


def make_csv_export():
    rows = []
    for case_number, case_type, plaintiff, defendant, judge, status in (
        ('1446/6', 'مدنية', 'أحمد', 'محمد', 'القاضي الأول', 'منظورة'),
        ('1446/7', 'جسيمة', 'علي', 'سالم', 'القاضي الثاني', 'منظورة'),
    ):
        fields = [''] * 41
        fields[1] = 'نوع القضية الأساسي'
        fields[8] = 'برئاسة القاضي'
        fields[14] = 'رقم القضية'
        fields[16] = '2025/07/07'
        fields[17] = '2025/07/21'
        fields[19] = status
        fields[20] = judge
        fields[21] = defendant
        fields[22] = plaintiff
        fields[23] = '2025/01/01'
        fields[24] = 'موضوع القضية'
        fields[25] = case_type
        fields[26] = case_number

        inner = StringIO()
        csv.writer(inner, lineterminator='').writerow(fields)
        outer = StringIO()
        csv.writer(outer, lineterminator='').writerow([inner.getvalue()])
        rows.append(outer.getvalue())
    return '\n'.join(rows).encode('cp1256')


class CaseRecordTests(unittest.TestCase):
    def setUp(self):
        self.csv_bytes = make_csv_export()
        with case_records._records_cache_lock:
            case_records._records_cache = None
            case_records._records_cache_expires_at = 0

    @patch.object(case_records, 'urlopen')
    def test_loads_records_from_google_drive(self, urlopen):
        urlopen.return_value = BytesIO(self.csv_bytes)

        with patch.dict(os.environ, {'GOOGLE_DRIVE_FILE_ID': 'unit-test-id'}):
            records = case_records.load_case_records()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['رقم القضية'], '1446/6')
        self.assertEqual(records[0]['نوع القضية'], 'مدنية')
        self.assertEqual(records[0]['موضوعها'], 'موضوع القضية')
        self.assertEqual(records[0]['الجلسة القادمة'], '2025/07/21')
        request = urlopen.call_args.args[0]
        self.assertIn('unit-test-id', request.full_url)
        urlopen.assert_called_once_with(
            request,
            timeout=case_records.DOWNLOAD_TIMEOUT_SECONDS,
        )

    @patch.object(case_records, 'urlopen')
    def test_refreshes_records_after_the_cache_expires(self, urlopen):
        urlopen.side_effect = (
            BytesIO(self.csv_bytes),
            BytesIO(self.csv_bytes),
        )

        with patch.dict(os.environ, {'GOOGLE_DRIVE_FILE_ID': 'unit-test-id'}):
            first_records = case_records.load_case_records()
            with case_records._records_cache_lock:
                case_records._records_cache_expires_at = 0
            second_records = case_records.load_case_records()

        self.assertEqual(first_records, second_records)
        self.assertEqual(urlopen.call_count, 2)

    @patch.object(case_records, 'urlopen')
    def test_rejects_unavailable_google_drive_csv(self, urlopen):
        urlopen.side_effect = case_records.URLError('unavailable')

        with (
            patch.dict(os.environ, {'GOOGLE_DRIVE_FILE_ID': 'unit-test-id'}),
            self.assertRaisesRegex(case_records.CaseRecordsError, 'Google Drive'),
        ):
            case_records.load_case_records()

    def test_requires_google_drive_file_id_from_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                case_records.CaseRecordsError,
                'معرّف ملف سجل القضايا',
            ):
                case_records._download_case_csv()

    @patch.object(case_records, 'urlopen')
    def test_rejects_oversized_case_csv(self, urlopen):
        urlopen.return_value = BytesIO(b'x' * (case_records.MAX_CSV_BYTES + 1))

        with (
            patch.dict(os.environ, {'GOOGLE_DRIVE_FILE_ID': 'unit-test-id'}),
            self.assertRaises(case_records.CaseRecordsError),
        ):
            case_records.load_case_records()

    def test_lists_unique_case_types_and_judges(self):
        records = [
            {'نوع القضية': 'مدني', 'برئاسة القاضي': 'القاضي الأول'},
            {'نوع القضية': 'مدني', 'برئاسة القاضي': 'القاضي الأول'},
            {'نوع القضية': 'جزائي', 'برئاسة القاضي': 'القاضي الثاني'},
        ]

        self.assertEqual(case_records.available_case_types(records), ['جزائي', 'مدني'])
        self.assertEqual(
            case_records.available_judges(records),
            ['القاضي الأول', 'القاضي الثاني'],
        )

    def test_lists_court_case_types_in_the_requested_order(self):
        records = [
            {'نوع القضية': 'تعرض للانحراف'},
            {'نوع القضية': 'غير جسيمة'},
            {'نوع القضية': 'جسيمة'},
            {'نوع القضية': 'شخصية'},
            {'نوع القضية': 'مدنية'},
        ]

        self.assertEqual(
            case_records.available_case_types(records),
            [
                'مدنيــــة',
                'شخصيـــة',
                'جزائية (جسيمة)',
                'جزائية (غير جسيمة)',
                'جزائية (أحداث)',
            ],
        )
        self.assertTrue(
            case_records.case_type_matches(
                'تعرض للانحراف',
                'جزائية (أحداث)',
            )
        )

    def test_searches_by_case_number_or_party_with_type_and_judge_filters(self):
        records = [
            {
                'رقم القضية': '1446/6',
                'نوع القضية': 'مدني',
                'المدعي': 'أحمد',
                'المدعى عليه': 'محمد',
                'برئاسة القاضي': 'القاضي الأول',
            },
            {
                'رقم القضية': '1446/7',
                'نوع القضية': 'جزائي',
                'المدعي': 'علي',
                'المدعى عليه': 'سالم',
                'برئاسة القاضي': 'القاضي الثاني',
            },
        ]

        by_number = case_records.search_case_records(records, '1446/6', 'number')
        by_name = case_records.search_case_records(
            records,
            'سالم',
            'name',
            case_type='جزائي',
            judge='القاضي الثاني',
        )
        no_match = case_records.search_case_records(
            records,
            'سالم',
            'name',
            case_type='مدني',
        )

        self.assertEqual(by_number, [records[0]])
        self.assertEqual(by_name, [records[1]])
        self.assertEqual(no_match, [])

    def test_searches_cases_by_next_session_date(self):
        records = [
            {'رقم القضية': '1446/6', 'الجلسة القادمة': '2026/10/21'},
            {'رقم القضية': '1446/7', 'الجلسة القادمة': '2026-10-22'},
            {'رقم القضية': '1446/8', 'الجلسة القادمة': '٢١/١٠/٢٠٢٦'},
        ]

        sessions = case_records.search_sessions_by_date(records, '2026-10-21')

        self.assertEqual(
            [record['رقم القضية'] for record in sessions],
            ['1446/6', '1446/8'],
        )
        self.assertEqual(case_records.search_sessions_by_date(records, '2026-13-40'), [])

    @patch.object(case_records, '_create_sessions_pdf')
    def test_generates_session_pdf_for_all_records(self, create_pdf):
        records = [{}] * 101

        case_records.generate_sessions_pdf(records, '2026-10-21')

        create_pdf.assert_called_once_with(records, '2026-10-21')

    def test_case_number_search_accepts_sequence_then_year_order(self):
        records = [{'رقم القضية': '1446/6'}]

        matches = case_records.search_case_records(records, '6/1446', 'number')
        arabic_digit_matches = case_records.search_case_records(
            records,
            '٦/١٤٤٦',
            'number',
        )

        self.assertEqual(matches, records)
        self.assertEqual(arabic_digit_matches, records)

    def test_uses_only_the_bundled_sultan_medium_font(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundled_font = Path(temporary_directory) / 'sultan-medium.ttf'
            bundled_font.touch()

            with patch.object(case_records, 'DEFAULT_FONT_PATH', bundled_font):
                self.assertEqual(case_records._font_path(), bundled_font)

    def test_bundled_report_font_is_sultan_medium(self):
        case_records._register_arabic_font()

        self.assertEqual(case_records.DEFAULT_FONT_PATH.name, 'sultan-medium.ttf')
        self.assertEqual(case_records._font_path(), case_records.DEFAULT_FONT_PATH)
        self.assertIn(
            b'Sultan',
            case_records.pdfmetrics.getFont(
                case_records.FONT_NAME
            ).face.name,
        )

    def test_bundled_report_font_supports_shaped_arabic_text(self):
        case_records._register_arabic_font()
        font = case_records.pdfmetrics.getFont(case_records.FONT_NAME)
        shaped_text = case_records._shape_pdf_text(
            'القاضي المختص مدنية جزائية',
            10,
        )

        self.assertTrue(font.shapable)
        self.assertTrue(hasattr(shaped_text, '__shapeData__'))
        self.assertGreater(
            case_records._pdf_text_width('القاضي المختص مدنية جزائية', 10),
            0,
        )

    def test_converts_pdf_digits_to_arabic_indic(self):
        self.assertEqual(
            '1446/6 - 2026/10/27'.translate(
                case_records.PDF_DIGIT_TRANSLATION
            ),
            '١٤٤٦/٦ - ٢٠٢٦/١٠/٢٧',
        )

    def test_pdf_shaping_receives_arabic_indic_digits(self):
        case_records._register_arabic_font()
        with patch.object(
            case_records,
            'shapeStr',
            return_value='shaped',
        ) as shape:
            case_records._shape_pdf_text('القضية 1446/6', 10)

        rendered_text = shape.call_args.args[0]
        self.assertIn('٦/٦٤٤١', rendered_text)
        self.assertNotRegex(rendered_text, r'[0-9]')

    def test_pdf_date_shaping_keeps_day_first_visual_order(self):
        case_records._register_arabic_font()
        gregorian_text = case_records._render_pdf_text(
            '31/10/2026',
            reverse_numeric_runs=True,
        )
        hijri_text = case_records._render_pdf_text(
            '20/05/1448',
            reverse_numeric_runs=True,
        )
        self.assertEqual(gregorian_text, '١٣/٠١/٦٢٠٢')
        self.assertEqual(hijri_text, '٠٢/٥٠/٨٤٤١')
        gregorian = case_records.shapeStr(
            gregorian_text,
            case_records.FONT_NAME,
            24,
        )
        hijri = case_records.shapeStr(
            hijri_text,
            case_records.FONT_NAME,
            24,
        )

        self.assertEqual(str(gregorian), '٢٠٢٦/١٠/٣١')
        self.assertEqual(str(hijri), '١٤٤٨/٠٥/٢٠')
        for value, expected in (
            ('31', '٣١'),
            ('10', '١٠'),
            ('20', '٢٠'),
            ('05', '٠٥'),
        ):
            rendered = case_records._render_pdf_text(
                value,
                reverse_numeric_runs=True,
            )
            shaped = case_records.shapeStr(
                rendered,
                case_records.FONT_NAME,
                24,
            )
            self.assertEqual(str(shaped), expected)

    def test_pdf_text_keeps_arabic_logical_order(self):
        self.assertEqual(
            case_records._pdf_text('حسين محمد ١٤٤٦/٦'),
            'حسين محمد ٦/٦٤٤١',
        )
        self.assertEqual(
            case_records._pdf_text('٣١/١٠/٢٠٢٦'),
            '٣١/١٠/٢٠٢٦',
        )

    def test_uses_sultan_for_supported_text_and_rejects_unsupported_glyphs(self):
        case_records._register_arabic_font()

        self.assertEqual(
            case_records._pdf_font_for_text('القضية ١٤٤٦'),
            case_records.FONT_NAME,
        )
        self.assertEqual(
            case_records._pdf_font_for_text('القضية ١٤٤٦/٦'),
            case_records.FONT_NAME,
        )
        self.assertEqual(
            case_records._pdf_font_for_text('١٤٤٦+٦'),
            case_records.FONT_NAME,
        )

    def test_rejects_text_without_a_supported_sultan_glyph(self):
        font = SimpleNamespace(face=SimpleNamespace(charWidths={}))
        with (
            patch.object(case_records, '_register_arabic_font'),
            patch.object(
                case_records.pdfmetrics,
                'getFont',
                return_value=font,
            ),
        ):
            with self.assertRaisesRegex(
                case_records.CaseRecordsError,
                'خط Sultan Medium لا يدعم',
            ):
                case_records._pdf_font_for_text(chr(0x10FFFF))

    def test_displays_judge_names_with_only_the_scale_prefix(self):
        self.assertEqual(
            case_records.display_judge_name('القاضي الأول'),
            '⚖️القاضي الأول',
        )
        self.assertEqual(
            case_records.display_judge_name('⚖️القاضي الأول'),
            '⚖️القاضي الأول',
        )
        self.assertEqual(case_records.display_judge_name(''), '-')

    def test_sessions_header_includes_weekday_and_both_calendar_dates(self):
        self.assertEqual(
            case_records._sessions_header_text('2026-10-21'),
            'قائمة الجلسات ليوم الأربعاء بتاريخ: 21/10/2026م '
            'الموافق 10/05/1448هـ',
        )

    def test_template_date_components_leave_preprinted_years_out(self):
        gregorian_date = case_records.datetime.strptime(
            '2026-10-21',
            '%Y-%m-%d',
        ).date()

        self.assertEqual(
            case_records._gregorian_day_month(gregorian_date),
            '21/10',
        )
        self.assertEqual(
            case_records._hijri_day_month(gregorian_date),
            '10/05',
        )

    def test_draws_long_party_names_on_one_line_and_fits_the_box(self):
        case_records._register_arabic_font()
        document = Mock()
        long_name = ' '.join(['اسم طويل للطرف'] * 80)

        case_records._draw_single_line_fit(
            document,
            long_name,
            380,
            648,
            205,
            max_size=24,
        )

        document.drawString.assert_called_once()
        font_size = document.setFont.call_args.args[1]
        x, y, shaped_text = document.drawString.call_args.args
        text_width = case_records._shaped_text_width(shaped_text, font_size)
        self.assertAlmostEqual(x, 380 - text_width / 2)
        self.assertEqual(y, 648)
        self.assertTrue(hasattr(shaped_text, '__shapeData__'))
        self.assertLess(font_size, 24)
        self.assertLessEqual(
            text_width,
            205,
        )

    def test_generates_a_case_pdf_with_the_official_template(self):
        if not case_records.CASE_INFO_TEMPLATE.is_file():
            self.skipTest('The official case-information PDF is not in this checkout.')
        candidates = (
            str(case_records.DEFAULT_FONT_PATH),
        )
        if not any(path and Path(path).is_file() for path in candidates):
            self.skipTest('No Arabic TrueType font is installed on this runner.')

        record = {
            'رقم القضية': '1446/6',
            'تاريخ ورودها': '2025/01/02',
            'موضوعها': 'موضوع الدعوى',
            'نوع القضية': 'مدنية',
            'المدعي': 'المدعي',
            'المدعى عليه': 'المدعى عليه',
            'حالة القضية': 'منظورة',
            'برئاسة القاضي': 'القاضي الأول',
            'الجلسة القادمة': '2025/01/03',
        }
        output = case_records.generate_case_pdf(record)

        pdf = case_records.PdfReader(output)
        template = case_records.PdfReader(str(case_records.CASE_INFO_TEMPLATE))
        self.assertEqual(len(pdf.pages), len(template.pages))
        output_page = pdf.pages[0]
        extracted_text = output_page.extract_text()
        normalized_text = unicodedata.normalize('NFKC', extracted_text)
        for value in ('موضوع الدعوى', 'مدنية', 'منظورة', 'القاضي الأول'):
            self.assertIn(value, normalized_text)
        self.assertRegex(extracted_text, r'[٠-٩]')
        report_fonts = [
            str(font.get_object().get('/BaseFont', ''))
            for font in output_page['/Resources']['/Font'].values()
        ]
        font_name = case_records.pdfmetrics.getFont(
            case_records.FONT_NAME
        ).face.name.decode('ascii')
        self.assertTrue(any(font_name in name for name in report_fonts))
        content = output_page.get_contents().get_data()
        self.assertRegex(content, rb'/F\d+\+0 30 Tf')
        self.assertRegex(content, rb'/F\d+\+0 24 Tf')

    def test_long_case_field_continues_on_additional_official_form_pages(self):
        long_defendant = 'مدعىعليه' * 80
        record = {
            'رقم القضية': '1446/6',
            'تاريخ ورودها': '2025/01/02',
            'موضوعها': 'موضوع الدعوى',
            'نوع القضية': 'مدنية',
            'المدعي': 'المدعي',
            'المدعى عليه': long_defendant,
            'حالة القضية': 'منظورة',
            'برئاسة القاضي': 'القاضي الأول',
            'الجلسة القادمة': '2025/01/03',
        }
        lines = case_records._wrap_pdf_text(
            long_defendant,
            357,
            30,
        )
        expected_page_count = (len(lines) + 1) // 2

        output = case_records.generate_case_pdf(record)
        pdf = case_records.PdfReader(output)
        template = case_records.PdfReader(str(case_records.CASE_INFO_TEMPLATE))

        self.assertGreater(expected_page_count, 1)
        self.assertEqual(len(pdf.pages), expected_page_count)
        for page in pdf.pages:
            self.assertEqual(
                float(page.mediabox.width),
                float(template.pages[0].mediabox.width),
            )
            self.assertEqual(
                float(page.mediabox.height),
                float(template.pages[0].mediabox.height),
            )

    def test_generates_a_session_list_on_the_official_template(self):
        if not case_records.SESSIONS_TEMPLATE.is_file():
            self.skipTest('The official sessions-list PDF is not in this checkout.')
        candidates = (
            str(case_records.DEFAULT_FONT_PATH),
        )
        if not any(path and Path(path).is_file() for path in candidates):
            self.skipTest('No Arabic TrueType font is installed on this runner.')

        records = [
            {
                'رقم القضية': f'1446/{index}',
                'نوع القضية': 'مدني',
                'موضوعها': (
                    'موضوعطويل' * 50
                    if index == 1
                    else 'موضوع القضية طويل للعرض في النموذج الرسمي'
                ),
                'المدعي': 'المدعي',
                'المدعى عليه': 'المدعى عليه',
                'برئاسة القاضي': 'القاضي الأول',
                'الجلسة القادمة': '2026/10/21',
            }
            for index in range(1, 102)
        ]
        output = case_records.generate_sessions_pdf(records, '2026-10-21')

        pdf = case_records.PdfReader(output)
        self.assertEqual(len(pdf.pages), 6)
        first_page_text = pdf.pages[0].extract_text()
        self.assertEqual(first_page_text.count('2026'), 1)
        self.assertEqual(first_page_text.count('1448'), 1)
        self.assertGreaterEqual(
            len(re.findall(r'[٠-٩]{2}', first_page_text)),
            4,
        )
        self.assertIn('١٢', first_page_text)
        self.assertIn('٠١', first_page_text)
        self.assertIn('٥٠', first_page_text)
        placements = []

        def capture_placement(text, _, text_matrix, __, font_size):
            placements.append((text, text_matrix[4], text_matrix[5], font_size))

        pdf.pages[0].extract_text(visitor_text=capture_placement)
        date_components = [
            text.strip()
            for text, _, y, font_size in placements
            if (
                abs(y - 664.18) < 0.1
                and font_size == case_records.SESSIONS_HEADER_FONT_SIZE
                and re.fullmatch(r'[٠-٩]{2}', text.strip())
            )
        ]
        self.assertEqual(len(date_components), 4)
        self.assertEqual(
            date_components,
            ['١٢', '٠١', '٠١', '٥٠'],
        )
        self.assertTrue(any(
            'القاضي الأول' in unicodedata.normalize('NFKC', text)
            and x < 394.51
            and abs(y - 689.74) < 0.1
            and font_size == case_records.SESSIONS_HEADER_FONT_SIZE
            for text, x, y, font_size in placements
        ))
        report_fonts = [
            str(font.get_object().get('/BaseFont', ''))
            for font in pdf.pages[0]['/Resources']['/Font'].values()
        ]
        font_name = case_records.pdfmetrics.getFont(
            case_records.FONT_NAME
        ).face.name.decode('ascii')
        self.assertTrue(any(font_name in name for name in report_fonts))
        template = case_records.PdfReader(str(case_records.SESSIONS_TEMPLATE))
        template_text_operations = (
            template.pages[0].get_contents().get_data().count(b'Tj')
        )
        output_text_operations = 0
        for page in pdf.pages:
            output_text_operations += page.get_contents().get_data().count(b'Tj')
        self.assertGreater(
            output_text_operations,
            len(pdf.pages) * template_text_operations + 400,
        )

        case_records._register_arabic_font()
        long_subject = 'موضوعطويل' * 50
        wrapped_subject = case_records._wrap_pdf_text(
            long_subject,
            460.9 - 367.03 - 6,
            case_records.SESSIONS_BODY_FONT_SIZE,
        )
        self.assertEqual(''.join(wrapped_subject), long_subject)

    def test_separates_session_pages_by_judge(self):
        records = [
            {
                'رقم القضية': '1446/1',
                'نوع القضية': 'مدني',
                'موضوعها': 'الموضوع الأول',
                'المدعي': 'المدعي الأول',
                'المدعى عليه': 'المدعى عليه الأول',
                'برئاسة القاضي': 'القاضي الأول',
            },
            {
                'رقم القضية': '1446/2',
                'نوع القضية': 'مدني',
                'موضوعها': 'الموضوع الثاني',
                'المدعي': 'المدعي الثاني',
                'المدعى عليه': 'المدعى عليه الثاني',
                'برئاسة القاضي': 'القاضي الثاني',
            },
            {
                'رقم القضية': '1446/3',
                'نوع القضية': 'مدني',
                'موضوعها': 'الموضوع الثالث',
                'المدعي': 'المدعي الثالث',
                'المدعى عليه': 'المدعى عليه الثالث',
                'برئاسة القاضي': 'القاضي الأول',
            },
        ]

        pdf = case_records.PdfReader(
            case_records.generate_sessions_pdf(records, '2026-10-21')
        )

        self.assertEqual(len(pdf.pages), 2)
        first_page_text = pdf.pages[0].extract_text()
        second_page_text = pdf.pages[1].extract_text()
        first_page_normalized = unicodedata.normalize('NFKC', first_page_text)
        second_page_normalized = unicodedata.normalize('NFKC', second_page_text)
        self.assertIn('القاضي الأول', first_page_normalized)
        self.assertTrue(any(
            number in first_page_text
            for number in ('١/٦٤٤١', '١٤٤٦/١')
        ))
        self.assertTrue(any(
            number in first_page_text
            for number in ('٣/٦٤٤١', '١٤٤٦/٣')
        ))
        self.assertNotIn('القاضي الثاني', first_page_normalized)
        self.assertNotIn('١٤٤٦/٢', first_page_text)
        self.assertNotIn('٢/٦٤٤١', first_page_text)
        self.assertIn('القاضي الثاني', second_page_normalized)
        self.assertTrue(any(
            number in second_page_text
            for number in ('٢/٦٤٤١', '١٤٤٦/٢')
        ))
        self.assertNotIn('القاضي الأول', second_page_normalized)

    def test_generates_an_attendance_notice_on_the_official_template(self):
        if not case_records.ATTENDANCE_TEMPLATE.is_file():
            self.skipTest('The official attendance PDF is not in this checkout.')
        candidates = (
            str(case_records.DEFAULT_FONT_PATH),
        )
        if not any(path and Path(path).is_file() for path in candidates):
            self.skipTest('No Arabic TrueType font is installed on this runner.')

        output = case_records.generate_attendance_ad_pdf(
            '1446/6',
            'المدعي',
            'المدعى عليه',
            '2026-10-27',
            'القاضي الأول',
        )

        pdf = case_records.PdfReader(output)
        self.assertEqual(len(pdf.pages), 1)
        extracted_text = pdf.pages[0].extract_text()
        self.assertEqual(extracted_text.count('2026'), 2)
        self.assertEqual(extracted_text.count('1448'), 2)
        session_date = case_records.datetime.strptime(
            '2026-10-27',
            '%Y-%m-%d',
        ).date()
        self.assertIn(
            case_records._render_pdf_text(
                case_records._gregorian_full_date(session_date),
                reverse_numeric_runs=True,
            ),
            extracted_text,
        )
        self.assertIn(
            case_records._render_pdf_text(
                case_records._hijri_full_date(session_date),
                reverse_numeric_runs=True,
            ),
            extracted_text,
        )
        preparation_date = case_records.datetime.now(
            case_records.COURT_TIMEZONE,
        ).date()
        for date_value in (
            case_records._gregorian_full_date(preparation_date),
            case_records._hijri_full_date(preparation_date),
        ):
            self.assertIn(
                case_records._render_pdf_text(
                    date_value,
                    reverse_numeric_runs=True,
                ),
                extracted_text,
            )
        self.assertIn(
            'القاضي الأول',
            unicodedata.normalize('NFKC', extracted_text),
        )
        placements = []

        def capture_placement(text, _, text_matrix, __, font_size):
            placements.append((text, text_matrix[4], text_matrix[5], font_size))

        pdf.pages[0].extract_text(visitor_text=capture_placement)
        session_date_fields = [
            text.strip()
            for text, _, y, font_size in placements
            if (
                abs(y - 521.47) < 0.1
                and font_size == 24
                and re.fullmatch(
                    r'[٠-٩]{2}/[٠-٩]{2}/[٠-٩]{4}',
                    text.strip(),
                )
            )
        ]
        preparation_date_fields = [
            text.strip()
            for text, _, y, font_size in placements
            if (
                abs(y - 450.31) < 0.1
                and font_size == 24.96
                and re.fullmatch(
                    r'[٠-٩]{2}/[٠-٩]{2}/[٠-٩]{4}',
                    text.strip(),
                )
            )
        ]
        self.assertEqual(len(session_date_fields), 2)
        self.assertEqual(len(preparation_date_fields), 2)
        self.assertTrue(any(
            'القاضي الأول' in unicodedata.normalize('NFKC', text)
            and x < 214.25
            and abs(y - 382.49) < 0.1
            and font_size == 18
            for text, x, y, font_size in placements
        ))
        template = case_records.PdfReader(str(case_records.ATTENDANCE_TEMPLATE))
        template_text_operations = (
            template.pages[0].get_contents().get_data().count(b'Tj')
        )
        output_text_operations = (
            pdf.pages[0].get_contents().get_data().count(b'Tj')
        )
        self.assertGreater(output_text_operations, template_text_operations)

    def test_rejects_malformed_case_csv(self):
        with self.assertRaises(case_records.CaseRecordsError):
            case_records._parse_case_records(b'not-a-court-report')

    @patch.object(case_records, '_create_pdf')
    def test_attendance_notice_includes_presiding_judge(self, create_pdf):
        case_records.generate_attendance_ad_pdf(
            '1446/6',
            'أحمد',
            'محمد',
            '2026-11-10',
            'القاضي الأول',
        )

        fields = create_pdf.call_args.args[1]
        self.assertEqual(
            fields,
            {
                'plaintiff': 'أحمد',
                'defendant': 'محمد',
                'session_date': '2026-11-10',
                'judge': 'القاضي الأول',
            },
        )
        self.assertEqual(
            create_pdf.call_args.kwargs['template_layout'],
            'attendance_ad',
        )

    @patch.object(case_records, '_create_pdf')
    def test_case_details_label_presiding_judge(self, create_pdf):
        record = {
            'رقم القضية': '1446/6',
            'تاريخ ورودها': '2025/01/02',
            'موضوعها': 'موضوع القضية',
            'نوع القضية': 'مدني',
            'المدعي': 'أحمد',
            'المدعى عليه': 'محمد',
            'حالة القضية': 'منظورة',
            'الجلسة القادمة': '2026/10/27',
            'برئاسة القاضي': 'القاضي الأول',
        }
        case_records.generate_case_pdf(record)

        fields = create_pdf.call_args.args[1]
        self.assertEqual(
            fields,
            [
                ('القاضي المختص:', 'القاضي الأول'),
                ('رقم القضية:', '1446/6'),
                ('تاريخ الورود:', '2025/01/02'),
                ('موضوع القضية:', 'موضوع القضية'),
                ('نوع القضية:', 'مدني'),
                ('المدعي:', 'أحمد'),
                ('المدعى عليه:', 'محمد'),
                ('آخر قرار للقضية:', 'منظورة'),
                ('تاريخ الجلسة القادمة:', '2026/10/27'),
            ],
        )
        self.assertEqual(create_pdf.call_args.kwargs['template_layout'], 'case_info')


if __name__ == '__main__':
    unittest.main()
