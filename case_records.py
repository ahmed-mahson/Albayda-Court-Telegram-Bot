import csv
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hijridate import Gregorian
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, shapeStr
from reportlab.pdfgen import canvas

GOOGLE_DRIVE_DOWNLOAD_URL = 'https://drive.google.com/uc?'
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_CSV_ROWS = 100_000
RECORD_CACHE_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 30
FONT_NAME = 'SultanMedium'
PDF_DIGIT_TRANSLATION = str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')
COURT_TIMEZONE = timezone(timedelta(hours=3))
ARABIC_WEEKDAYS = (
    'الاثنين',
    'الثلاثاء',
    'الأربعاء',
    'الخميس',
    'الجمعة',
    'السبت',
    'الأحد',
)
DEFAULT_FONT_PATH = (
    Path(__file__).resolve().parent / 'assets' / 'sultan-medium.ttf'
)
CASE_INFO_TEMPLATE = Path(__file__).resolve().parent / 'معلومات القضية.pdf'
SESSIONS_TEMPLATE = Path(__file__).resolve().parent / 'قائمة الجلسات.pdf'
ATTENDANCE_TEMPLATE = Path(__file__).resolve().parent / 'نموذج الاعلان.pdf'
SEARCH_REPORT_TEMPLATE = Path(__file__).resolve().parent / 'الكليشة.pdf'
CASE_TYPE_DISPLAY_NAMES = {
    'مدنية': 'مدنيــــة',
    'شخصية': 'شخصيـــة',
    'جسيمة': 'جزائية (جسيمة)',
    'غير جسيمة': 'جزائية (غير جسيمة)',
    'تعرض للانحراف': 'جزائية (أحداث)',
}
CASE_TYPE_ORDER = (
    'مدنية',
    'شخصية',
    'جسيمة',
    'غير جسيمة',
    'تعرض للانحراف',
)
CASE_TYPE_SOURCE_BY_DISPLAY_NAME = {
    display_name: source_name
    for source_name, display_name in CASE_TYPE_DISPLAY_NAMES.items()
}
CASE_INFO_ROW_LAYOUT = (
    ('القاضي المختص:', 609.94, 30),
    ('رقم القضية:', 550.63, 30),
    ('تاريخ الورود:', 491.35, 30),
    ('موضوع القضية:', 431.95, 30),
    ('نوع القضية:', 372.65, 30),
    ('المدعي:', 313.37, 30),
    ('المدعى عليه:', 254.09, 30),
    ('آخر قرار للقضية:', 194.78, 30),
    ('تاريخ الجلسة القادمة:', 136.22, 24),
)
SESSIONS_TABLE_LEFT = 42.84
SESSIONS_TABLE_RIGHT = 552.58
SESSIONS_TABLE_TOP = 621.26
SESSIONS_TABLE_BOTTOM = 127.82
SESSIONS_TABLE_ROW_HEIGHT = 24.6
SESSIONS_TABLE_COLUMN_EDGES = (
    43.8,
    174.5,
    305.21,
    367.03,
    460.9,
    523.78,
    552.58,
)
SESSIONS_TABLE_SEQUENCE_CENTER = (
    SESSIONS_TABLE_COLUMN_EDGES[-2] + SESSIONS_TABLE_COLUMN_EDGES[-1]
) / 2
SESSIONS_HEADER_FONT_SIZE = 21.96
SESSIONS_BODY_FONT_SIZE = 8.5
SESSIONS_TABLE_COLUMNS = (
    ('case_number', 'رقم القضية', 460.9, 523.78),
    ('subject', 'موضوعها', 367.03, 460.9),
    ('case_type', 'نوع القضية', 305.21, 367.03),
    ('plaintiff', 'المدعي', 174.5, 305.21),
    ('defendant', 'المدعى عليه', 43.8, 174.5),
)
_records_cache = None
_records_cache_expires_at = 0
_records_cache_lock = threading.Lock()


class CaseRecordsError(Exception):
    """Raised when the case CSV or its PDF output is unavailable."""


def _cell_text(value):
    if value is None:
        return ''
    return str(value).strip()


def display_judge_name(value):
    name = _cell_text(value)
    if not name or name == '-':
        return name or '-'
    return name if name.startswith('⚖️') else f'⚖️{name}'


def _display_case_number(value):
    text = _cell_text(value)
    if not text or text == '-':
        return text or '-'
    return text


def _display_date(value):
    text = _cell_text(value)
    if not text or text == '-':
        return text or '-'
    normalized = _normalized_date(text)
    if normalized is None:
        return text
    gregorian_date = datetime.strptime(normalized, '%Y-%m-%d').date()
    return f'{gregorian_date.day:02}/{gregorian_date.month:02}/{gregorian_date.year:04}'


def _parse_case_records(content):
    try:
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('cp1256')

        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue

            wrapped_row = next(csv.reader([line], strict=True), [])
            if len(wrapped_row) != 1:
                raise CaseRecordsError(
                    f'تنسيق صف سجل القضايا غير صالح (السطر {line_number}).'
                )
            fields = next(csv.reader([wrapped_row[0]], strict=True), [])
            if len(fields) < 27:
                raise CaseRecordsError(
                    f'أعمدة صف سجل القضايا غير مكتملة (السطر {line_number}).'
                )
            if (
                fields[1].strip() != 'نوع القضية الأساسي'
                or fields[8].strip() != 'برئاسة القاضي'
                or fields[14].strip() != 'رقم القضية'
            ):
                raise CaseRecordsError(
                    'عناوين الأعمدة في ملف سجل القضايا لا تطابق التنسيق المتوقع.'
                )

            record = {
                'الجلسة السابقة': _cell_text(fields[16]),
                'الجلسة القادمة': _cell_text(fields[17]),
                'حالة القضية': _cell_text(fields[19]),
                'برئاسة القاضي': _cell_text(fields[20]),
                'المدعى عليه': _cell_text(fields[21]),
                'المدعي': _cell_text(fields[22]),
                'تاريخ ورودها': _cell_text(fields[23]),
                'موضوعها': _cell_text(fields[24]),
                'نوع القضية': _cell_text(fields[25]),
                'رقم القضية': _cell_text(fields[26]),
            }
            if any(record.values()):
                records.append(record)
                if len(records) > MAX_CSV_ROWS:
                    raise CaseRecordsError(
                        'ملف سجل القضايا يتجاوز الحد المسموح للصفوف.'
                    )
        if not records:
            raise CaseRecordsError('ملف سجل القضايا لا يحتوي على قضايا.')
        return records
    except CaseRecordsError:
        raise
    except (csv.Error, UnicodeDecodeError, IndexError) as exc:
        logging.warning('Court case CSV could not be read (%s)', type(exc).__name__)
        raise CaseRecordsError('تعذر قراءة ملف سجل القضايا المنشور.') from exc


def _download_case_csv():
    file_id = os.getenv('GOOGLE_DRIVE_FILE_ID', '').strip()
    if not file_id:
        raise CaseRecordsError('لم يتم إعداد معرّف ملف سجل القضايا في Google Drive.')

    url = GOOGLE_DRIVE_DOWNLOAD_URL + urlencode({
        'export': 'download',
        'id': file_id,
    })
    request = Request(url, headers={'User-Agent': 'AlbaydaCourtBot/1.0'})
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content = response.read(MAX_CSV_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        logging.warning('Google Drive case CSV is unavailable (%s)', type(exc).__name__)
        raise CaseRecordsError(
            'تعذر تنزيل سجل القضايا من Google Drive. '
            'تحقق من إتاحة الملف عبر الرابط واتصال الإنترنت.'
        ) from exc

    if len(content) > MAX_CSV_BYTES:
        raise CaseRecordsError('ملف سجل القضايا يتجاوز الحد المسموح للحجم.')
    return content


def load_case_records():
    global _records_cache, _records_cache_expires_at
    with _records_cache_lock:
        if _records_cache is not None and time.monotonic() < _records_cache_expires_at:
            return _records_cache

    content = _download_case_csv()
    records = _parse_case_records(content)

    with _records_cache_lock:
        _records_cache = records
        _records_cache_expires_at = time.monotonic() + RECORD_CACHE_SECONDS
    return records


def available_case_types(records):
    available = {
        record['نوع القضية']
        for record in records
        if record.get('نوع القضية')
    }
    ordered = [
        CASE_TYPE_DISPLAY_NAMES[case_type]
        for case_type in CASE_TYPE_ORDER
        if case_type in available
    ]
    other_types = sorted(
        case_type
        for case_type in available
        if case_type not in CASE_TYPE_DISPLAY_NAMES
    )
    return ordered + other_types


def case_type_matches(record_type, selected_type):
    source_type = CASE_TYPE_SOURCE_BY_DISPLAY_NAME.get(
        selected_type,
        selected_type,
    )
    return record_type == source_type


def available_judges(records):
    return sorted({
        record['برئاسة القاضي']
        for record in records
        if record.get('برئاسة القاضي')
    })


def _normalize_case_number(value):
    normalized = []
    for char in (value or '').strip():
        normalized.append(
            str(unicodedata.decimal(char)) if char.isdecimal() else char
        )
    return ''.join(normalized).casefold()


def search_case_records(records, query, method, case_type='all', judge='all', limit=5):
    normalized_query = (query or '').strip().casefold()
    if method == 'number':
        normalized_query = _normalize_case_number(normalized_query)
    if not normalized_query or method not in {'number', 'name'}:
        return []

    number_queries = {normalized_query}
    if method == 'number' and normalized_query.count('/') == 1:
        first, second = (part.strip() for part in normalized_query.split('/'))
        number_queries.add(f'{second}/{first}')
        number_queries = {
            _normalize_case_number(value)
            for value in number_queries
        }

    matches = []
    for record in records:
        if (
            case_type != 'all'
            and not case_type_matches(record.get('نوع القضية'), case_type)
        ):
            continue
        if judge != 'all' and record.get('برئاسة القاضي') != judge:
            continue

        if method == 'number':
            record_number = _normalize_case_number(record.get('رقم القضية', ''))
            matched = any(query in record_number for query in number_queries)
        else:
            matched = any(
                normalized_query in record.get(field, '').casefold()
                for field in ('المدعي', 'المدعى عليه')
            )
        if matched:
            matches.append(record)
            if len(matches) >= limit:
                break
    return matches


def _normalized_date(value):
    normalized_digits = []
    try:
        for char in (value or '').strip():
            normalized_digits.append(
                str(unicodedata.decimal(char)) if char.isdecimal() else char
            )
    except TypeError:
        return None
    value = ''.join(normalized_digits)
    for date_format in ('%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def search_sessions_by_date(records, sitting_date):
    target_date = _normalized_date(sitting_date)
    if target_date is None:
        return []
    return [
        record for record in records
        if _normalized_date(record.get('الجلسة القادمة')) == target_date
    ]


def _font_path():
    return DEFAULT_FONT_PATH if DEFAULT_FONT_PATH.is_file() else None


def _register_arabic_font():
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        path = _font_path()
        if path is None:
            raise CaseRecordsError(
                'لم يُعثر على خط يدعم العربية لإنشاء ملف PDF.'
            )
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
    if not pdfmetrics.getFont(FONT_NAME).shapable:
        raise CaseRecordsError(
            'تعذر تهيئة تشكيل الخط العربي. ثبّت اعتماديات الخط من requirements.txt.'
        )


def _pdf_text(value, reverse_numeric_runs=False):
    text = _cell_text(value)
    if not text or text == '-':
        return '-'
    return text


def _render_pdf_text(value, reverse_numeric_runs=False, keep_latin_digits=False):
    text = _cell_text(value) or '-'
    if not keep_latin_digits:
        text = text.translate(PDF_DIGIT_TRANSLATION)
    return _pdf_text(text, reverse_numeric_runs=reverse_numeric_runs)


def _pdf_font_for_text(value):
    _register_arabic_font()
    primary_font = pdfmetrics.getFont(FONT_NAME)
    missing_characters = ''.join(
        dict.fromkeys(
            character
            for character in value
            if (
                unicodedata.category(character) != 'Cf'
                and ord(character) not in primary_font.face.charWidths
            )
        )
    )
    if missing_characters:
        raise CaseRecordsError(
            f'تعذر إنشاء ملف PDF لأن خط Sultan Medium لا يدعم هذه الرموز: '
            f'{missing_characters}'
        )
    return FONT_NAME


def _pdf_text_width(value, font_size, reverse_numeric_runs=False, keep_latin_digits=False):
    text = _render_pdf_text(value, reverse_numeric_runs=reverse_numeric_runs, keep_latin_digits=keep_latin_digits)
    font_name = _pdf_font_for_text(text)
    shaped_text = shapeStr(text, font_name, font_size)
    return _shaped_text_width(shaped_text, font_size, font_name)


def _shaped_text_width(shaped_text, font_size, font_name=FONT_NAME):
    if not hasattr(shaped_text, '__shapeData__'):
        return pdfmetrics.stringWidth(shaped_text, font_name, font_size)
    return (
        sum(glyph.x_advance for glyph in shaped_text.__shapeData__)
        * font_size
        / 1000
    )


def _draw_pdf_text(
    document,
    x,
    baseline,
    value,
    font_size,
    alignment='left',
    reverse_numeric_runs=False,
    keep_latin_digits=False,
):
    text = _render_pdf_text(value, reverse_numeric_runs=reverse_numeric_runs, keep_latin_digits=keep_latin_digits)
    font_name = _pdf_font_for_text(text)
    shaped_text = shapeStr(text, font_name, font_size)
    width = _shaped_text_width(shaped_text, font_size, font_name)
    if alignment == 'center':
        x -= width / 2
    elif alignment == 'right':
        x -= width
    document.setFont(font_name, font_size)
    document.drawString(x, baseline, shaped_text)


def _cover_template_rectangle(document, x, y, width, height):
    document.saveState()
    document.setFillColorRGB(1, 1, 1)
    document.rect(x, y, width, height, stroke=0, fill=1)
    document.restoreState()


def _draw_centered_fit(
    document,
    value,
    center_x,
    baseline,
    max_width,
    max_size=10,
    reverse_numeric_runs=False,
    keep_latin_digits=False,
):
    text = _cell_text(value) or '-'
    words = text.split() or ['-']
    font_size = max_size
    while font_size >= 5.5:
        lines = []
        current_line = ''
        for word in words:
            candidate = f'{current_line} {word}'.strip()
            if _pdf_text_width(
                candidate,
                font_size,
                reverse_numeric_runs=reverse_numeric_runs,
                keep_latin_digits=keep_latin_digits,
            ) <= max_width:
                current_line = candidate
            elif current_line:
                lines.append(current_line)
                current_line = word
            else:
                lines.append(word)
                current_line = ''
        if current_line:
            lines.append(current_line)

        if len(lines) <= 3 and all(
            _pdf_text_width(
                line,
                font_size,
                reverse_numeric_runs=reverse_numeric_runs,
                keep_latin_digits=keep_latin_digits,
            ) <= max_width
            for line in lines
        ):
            document.setFont(FONT_NAME, font_size)
            leading = font_size * 1.15
            first_baseline = baseline + (len(lines) - 1) * leading / 2
            for index, line in enumerate(lines):
                _draw_pdf_text(
                    document,
                    center_x,
                    first_baseline - index * leading,
                    line,
                    font_size,
                    alignment='center',
                    reverse_numeric_runs=reverse_numeric_runs,
                    keep_latin_digits=keep_latin_digits,
                )
            return
        font_size -= 0.5
    raise CaseRecordsError('تعذر إدراج أحد حقول الجلسة في النموذج الرسمي.')


def _draw_single_line_fit(
    document,
    value,
    anchor_x,
    baseline,
    max_width,
    max_size=10,
    alignment='center',
    keep_latin_digits=False,
):
    text = _cell_text(value) or '-'
    text_width = _pdf_text_width(text, 1, keep_latin_digits=keep_latin_digits)
    if text_width <= 0 or max_width <= 0:
        raise CaseRecordsError('تعذر تنسيق أسماء أطراف القضية في الإعلان.')
    font_size = min(max_size, max_width / text_width)
    if font_size < max_size:
        font_size *= 1 - 1e-9
    document.setFont(FONT_NAME, font_size)
    _draw_pdf_text(
        document,
        anchor_x,
        baseline,
        value,
        font_size,
        alignment=alignment,
        keep_latin_digits=keep_latin_digits,
    )


def _wrap_pdf_text(value, max_width, font_size, keep_latin_digits=False):
    words = _cell_text(value).split() or ['-']
    lines = []
    current_line = ''

    def fits(text):
        return (
            _pdf_text_width(text, font_size, keep_latin_digits=keep_latin_digits) <= max_width
        )

    for word in words:
        candidate = f'{current_line} {word}'.strip()
        if fits(candidate):
            current_line = candidate
            continue
        if current_line:
            lines.append(current_line)
            current_line = ''

        if fits(word):
            current_line = word
            continue

        chunk = ''
        for character in word:
            if chunk and not fits(chunk + character):
                lines.append(chunk)
                chunk = character
            else:
                chunk += character
        current_line = chunk

    if current_line:
        lines.append(current_line)
    return lines


def _draw_case_info_fields(document, fields, width, height):
    values = dict(fields)
    scale_x = width / 595.32
    scale_y = height / 841.92
    right = 393 * scale_x
    max_width = 357 * scale_x
    field_lines = []
    
    # قائمة الحقول التي يُراد الحفاظ على أرقامها بالصيغة الأصلية (تقرير معلومات القضية)
    LATIN_DIGIT_FIELDS
