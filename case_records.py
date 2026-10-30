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
    if reverse_numeric_runs:
        return re.sub(
            r'[0-9٠-٩]+',
            lambda match: match.group(0)[::-1],
            text,
        )
    if not any(
        '\u0600' <= ch <= '\u06FF'
        and unicodedata.category(ch).startswith('L')
        for ch in text
    ):
        return text

    number_token = r'[0-9٠-٩]+(?:[/:.-][0-9٠-٩]+)*'
    # HarfBuzz treats Arabic-Indic digit runs as RTL; reverse them for LTR display.
    return re.sub(
        number_token,
        lambda match: match.group(0)[::-1],
        text,
    )


def _render_pdf_text(value, reverse_numeric_runs=False):
    text = _cell_text(value).translate(PDF_DIGIT_TRANSLATION) or '-'
    return _pdf_text(text, reverse_numeric_runs=reverse_numeric_runs)


def _shape_pdf_text(value, font_size):
    text = _render_pdf_text(value)
    return shapeStr(text, _pdf_font_for_text(text), font_size)


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


def _pdf_text_width(value, font_size, reverse_numeric_runs=False):
    text = _render_pdf_text(value, reverse_numeric_runs=reverse_numeric_runs)
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
):
    text = _render_pdf_text(value, reverse_numeric_runs=reverse_numeric_runs)
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
):
    text = _cell_text(value) or '-'
    text_width = _pdf_text_width(text, 1)
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
    )


def _wrap_pdf_text(value, max_width, font_size):
    words = _cell_text(value).split() or ['-']
    lines = []
    current_line = ''

    def fits(text):
        return (
            _pdf_text_width(text, font_size) <= max_width
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
    for label, baseline, font_size in CASE_INFO_ROW_LAYOUT:
        lines = _wrap_pdf_text(
            values.get(label, '-') or '-',
            max_width,
            font_size,
        )
        field_lines.append((
            baseline * scale_y,
            font_size,
            [
                lines[offset:offset + 2]
                for offset in range(0, len(lines), 2)
            ] or [['-']],
        ))

    page_count = max(len(chunks) for _, _, chunks in field_lines)
    for page_index in range(page_count):
        if page_index:
            document.showPage()
        for baseline, font_size, chunks in field_lines:
            if page_index >= len(chunks):
                continue
            lines = chunks[page_index]
            leading = font_size
            first_baseline = baseline + (len(lines) - 1) * leading / 2
            document.setFont(FONT_NAME, font_size)
            for line_index, line in enumerate(lines):
                _draw_pdf_text(
                    document,
                    right,
                    first_baseline - line_index * leading,
                    line,
                    font_size,
                    alignment='right',
                )


def _session_row_fragments(record):
    font_size = SESSIONS_BODY_FONT_SIZE
    leading = font_size * 1.15
    padding = 3.5
    body_height = SESSIONS_TABLE_TOP - SESSIONS_TABLE_BOTTOM
    lines_per_fragment = max(1, int((body_height - padding) / leading))
    wrapped = {
        name: _wrap_pdf_text(
            _cell_text(record.get(field)) or '-',
            right - left - 6,
            font_size,
        )
        for name, field, left, right in SESSIONS_TABLE_COLUMNS
    }
    max_line_count = max(
        (len(lines) for lines in wrapped.values()),
        default=1,
    )
    fragment_count = max(
        1,
        (max_line_count + lines_per_fragment - 1) // lines_per_fragment,
    )
    fragments = []
    for fragment_index in range(fragment_count):
        start = fragment_index * lines_per_fragment
        end = start + lines_per_fragment
        fields = {
            name: lines[start:end]
            for name, lines in wrapped.items()
        }
        line_count = max((len(lines) for lines in fields.values()), default=1)
        height = max(
            SESSIONS_TABLE_ROW_HEIGHT,
            line_count * leading + padding,
        )
        fragments.append({
            'fields': fields,
            'height': height,
            'first': fragment_index == 0,
        })
    return fragments


def _hijri_date(gregorian_date):
    try:
        hijri_date = Gregorian(
            gregorian_date.year,
            gregorian_date.month,
            gregorian_date.day,
        ).to_hijri()
    except (OverflowError, ValueError) as exc:
        raise CaseRecordsError('تعذر تحويل التاريخ الميلادي إلى هجري.') from exc
    return f'{hijri_date.day:02}/{hijri_date.month:02}/{hijri_date.year}'


def _gregorian_day_month(gregorian_date):
    return f'{gregorian_date.day:02}/{gregorian_date.month:02}'


def _hijri_day_month(gregorian_date):
    return '/'.join(_hijri_date(gregorian_date).split('/')[:2])


def _gregorian_full_date(gregorian_date):
    return (
        f'{gregorian_date.day:02}/'
        f'{gregorian_date.month:02}/'
        f'{gregorian_date.year:04}'
    )


def _hijri_full_date(gregorian_date):
    hijri_day, hijri_month, hijri_year = _hijri_date(gregorian_date).split('/')
    return f'{hijri_day}/{hijri_month}/{hijri_year}'


def _sessions_header_text(sitting_date):
    normalized_date = _normalized_date(sitting_date)
    if normalized_date is None:
        raise CaseRecordsError('تاريخ الجلسة غير صالح لإنشاء القائمة.')
    gregorian_date = datetime.strptime(normalized_date, '%Y-%m-%d').date()
    weekday = ARABIC_WEEKDAYS[gregorian_date.weekday()]
    gregorian_text = (
        f'{gregorian_date.day:02}/'
        f'{gregorian_date.month:02}/'
        f'{gregorian_date.year:04}'
    )
    return (
        f'قائمة الجلسات ليوم {weekday} بتاريخ: {gregorian_text}م '
        f'الموافق {_hijri_date(gregorian_date)}هـ'
    )


def _merge_template_overlay(title, template, overlay_stream, repeat_template_page=False):
    try:
        overlay = PdfReader(overlay_stream)
        writer = PdfWriter()
        if repeat_template_page:
            base_writer = PdfWriter()
            base_writer.add_page(template.pages[0])
            base_stream = BytesIO()
            base_writer.write(base_stream)
            base_pdf = base_stream.getvalue()
            for overlay_page in overlay.pages:
                repeated_template = PdfReader(BytesIO(base_pdf))
                writer.add_page(repeated_template.pages[0])
                writer.pages[-1].merge_page(overlay_page)
        else:
            for index, template_page in enumerate(template.pages):
                writer.add_page(template_page)
                if index == 0 and overlay.pages:
                    writer.pages[-1].merge_page(overlay.pages[0])
        writer.add_metadata({'/Title': title})
        result = BytesIO()
        writer.write(result)
        result.seek(0)
        return result
    except (OSError, PdfReadError, ValueError) as exc:
        logging.warning('Could not apply PDF template fields (%s)', type(exc).__name__)
        raise CaseRecordsError('تعذر تجهيز التقرير على النموذج الرسمي.') from exc


def _create_pdf(title, fields, template_path, template_layout=None):
    _register_arabic_font()
    template_path = Path(template_path)
    if not template_path.is_file():
        raise CaseRecordsError(
            f'تعذر العثور على النموذج الرسمي: {template_path.name}'
        )

    try:
        template = PdfReader(str(template_path))
        if not template.pages:
            raise CaseRecordsError(
                f'النموذج الرسمي لا يحتوي على صفحات: {template_path.name}'
            )

        page = template.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
    except CaseRecordsError:
        raise
    except (OSError, PdfReadError, ValueError) as exc:
        logging.warning(
            'Official PDF template could not be read (%s)',
            type(exc).__name__,
        )
        raise CaseRecordsError('تعذر قراءة النموذج الرسمي لملف PDF.') from exc

    output = BytesIO()
    document = canvas.Canvas(output, pagesize=(width, height))
    document.setTitle(title)
    if template_layout == 'case_info':
        _draw_case_info_fields(document, fields, width, height)
    elif template_layout == 'attendance_ad':
        session_date = datetime.strptime(fields['session_date'], '%Y-%m-%d').date()
        preparation_date = datetime.now(COURT_TIMEZONE).date()
        scale_x = width / 595.32
        scale_y = height / 841.92

        _cover_template_rectangle(
            document,
            249 * scale_x,
            516 * scale_y,
            45 * scale_x,
            31 * scale_y,
        )
        _cover_template_rectangle(
            document,
            46 * scale_x,
            516 * scale_y,
            51 * scale_x,
            31 * scale_y,
        )
        _cover_template_rectangle(
            document,
            298 * scale_x,
            444 * scale_y,
            114 * scale_x,
            31 * scale_y,
        )
        _cover_template_rectangle(
            document,
            123 * scale_x,
            444 * scale_y,
            55 * scale_x,
            31 * scale_y,
        )
        _draw_single_line_fit(
            document,
            fields['plaintiff'],
            380 * scale_x,
            648 * scale_y,
            205 * scale_x,
            max_size=24,
        )
        _draw_single_line_fit(
            document,
            fields['defendant'],
            380 * scale_x,
            603 * scale_y,
            205 * scale_x,
            max_size=24,
        )
        _draw_centered_fit(
            document,
            ARABIC_WEEKDAYS[session_date.weekday()],
            450 * scale_x,
            521.47 * scale_y,
            90 * scale_x,
            max_size=24,
        )
        _draw_centered_fit(
            document,
            _gregorian_full_date(session_date),
            310 * scale_x,
            521.47 * scale_y,
            115 * scale_x,
            max_size=24,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            _hijri_full_date(session_date),
            120 * scale_x,
            521.47 * scale_y,
            120 * scale_x,
            max_size=24,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            _gregorian_full_date(preparation_date),
            354 * scale_x,
            450.31 * scale_y,
            115 * scale_x,
            max_size=24.96,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            _hijri_full_date(preparation_date),
            190 * scale_x,
            450.31 * scale_y,
            120 * scale_x,
            max_size=24.96,
            reverse_numeric_runs=True,
        )
        _draw_single_line_fit(
            document,
            fields['judge'],
            211.25 * scale_x,
            382.49 * scale_y,
            150 * scale_x,
            max_size=18,
            alignment='right',
        )
    else:
        document.setFont(FONT_NAME, 12)
        _draw_pdf_text(
            document,
            width / 2,
            height - 40,
            title,
            12,
            alignment='center',
        )
        document.setFont(FONT_NAME, 10)
        y = height - 75
        for label, value in fields:
            _draw_pdf_text(
                document,
                width - 36,
                y,
                f'{label} {value}'[:160],
                10,
                alignment='right',
            )
            y -= 22
            if y < 36:
                document.showPage()
                document.setFont(FONT_NAME, 10)
                y = height - 36
    document.save()
    output.seek(0)

    if template_layout == 'case_info':
        return _merge_template_overlay(
            title,
            template,
            output,
            repeat_template_page=True,
        )
    if template_layout == 'attendance_ad':
        return _merge_template_overlay(title, template, output)

    try:
        overlay = PdfReader(output)
        writer = PdfWriter()
        for template_page in template.pages:
            writer.add_page(template_page)
        for report_page in overlay.pages:
            writer.add_page(report_page)
        writer.add_metadata({'/Title': title})
        result = BytesIO()
        writer.write(result)
        result.seek(0)
        return result
    except (OSError, PdfReadError, ValueError) as exc:
        logging.warning('Could not apply PDF report fields (%s)', type(exc).__name__)
        raise CaseRecordsError('تعذر تجهيز التقرير على النموذج الرسمي.') from exc


def _create_sessions_pdf(records, sitting_date):
    _register_arabic_font()
    if not SESSIONS_TEMPLATE.is_file():
        raise CaseRecordsError(
            f'تعذر العثور على النموذج الرسمي: {SESSIONS_TEMPLATE.name}'
        )
    try:
        template = PdfReader(str(SESSIONS_TEMPLATE))
        if not template.pages:
            raise CaseRecordsError(
                f'النموذج الرسمي لا يحتوي على صفحات: {SESSIONS_TEMPLATE.name}'
            )
        page = template.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
    except CaseRecordsError:
        raise
    except (OSError, PdfReadError, ValueError) as exc:
        logging.warning(
            'Official sessions PDF template could not be read (%s)',
            type(exc).__name__,
        )
        raise CaseRecordsError('تعذر قراءة نموذج قائمة الجلسات الرسمي.') from exc

    normalized_date = _normalized_date(sitting_date)
    if normalized_date is None:
        raise CaseRecordsError('تاريخ الجلسة غير صالح لإنشاء القائمة.')
    display_date = normalized_date.replace('-', '/')
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=(width, height))
    title = f'قائمة الجلسات ليوم {display_date}'
    document.setTitle(title)
    page_scale_x = width / 595.32
    page_scale_y = height / 841.92
    records_by_judge = {}
    for record in records:
        judge = _cell_text(record.get('برئاسة القاضي')) or 'غير محدد'
        records_by_judge.setdefault(judge, []).append(record)

    report_pages = []
    page_capacity = SESSIONS_TABLE_TOP - SESSIONS_TABLE_BOTTOM
    for judge in sorted(records_by_judge):
        current_page = []
        current_page_height = 0
        row_fragments = [
            fragment
            for record in records_by_judge[judge]
            for fragment in _session_row_fragments(record)
        ]
        for fragment in row_fragments:
            if (
                current_page
                and current_page_height + fragment['height'] > page_capacity
            ):
                report_pages.append((judge, current_page))
                current_page = []
                current_page_height = 0
            current_page.append(fragment)
            current_page_height += fragment['height']
        if current_page:
            report_pages.append((judge, current_page))

    for page_index, (judge_title, page_rows) in enumerate(report_pages):
        if page_index:
            document.showPage()
        _draw_single_line_fit(
            document,
            judge_title,
            391.5 * page_scale_x,
            689.74 * page_scale_y,
            150 * page_scale_x,
            max_size=SESSIONS_HEADER_FONT_SIZE,
            alignment='right',
        )
        header_baseline = 664.18 * page_scale_y
        gregorian_date = datetime.strptime(normalized_date, '%Y-%m-%d').date()
        weekday = ARABIC_WEEKDAYS[gregorian_date.weekday()]
        hijri_day, hijri_month = _hijri_day_month(gregorian_date).split('/')
        _draw_centered_fit(
            document,
            weekday,
            399 * page_scale_x,
            header_baseline,
            82 * page_scale_x,
            max_size=SESSIONS_HEADER_FONT_SIZE,
        )
        _draw_centered_fit(
            document,
            f'{gregorian_date.day:02}',
            281.2 * page_scale_x,
            header_baseline,
            25 * page_scale_x,
            max_size=SESSIONS_HEADER_FONT_SIZE,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            f'{gregorian_date.month:02}',
            254.7 * page_scale_x,
            header_baseline,
            25 * page_scale_x,
            max_size=SESSIONS_HEADER_FONT_SIZE,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            hijri_day,
            138.6 * page_scale_x,
            header_baseline,
            25 * page_scale_x,
            max_size=SESSIONS_HEADER_FONT_SIZE,
            reverse_numeric_runs=True,
        )
        _draw_centered_fit(
            document,
            hijri_month,
            112.1 * page_scale_x,
            header_baseline,
            25 * page_scale_x,
            SESSIONS_HEADER_FONT_SIZE,
            reverse_numeric_runs=True,
        )
        document.setFillColorRGB(1, 1, 1)
        document.rect(
            SESSIONS_TABLE_LEFT,
            SESSIONS_TABLE_BOTTOM,
            SESSIONS_TABLE_RIGHT - SESSIONS_TABLE_LEFT,
            SESSIONS_TABLE_TOP - SESSIONS_TABLE_BOTTOM,
            fill=1,
            stroke=0,
        )
        document.setFillColorRGB(0, 0, 0)
        row_boundaries = [SESSIONS_TABLE_TOP]
        cursor_y = SESSIONS_TABLE_TOP
        serial_number = 0
        for row_index, fragment in enumerate(page_rows):
            row_top = cursor_y
            cursor_y -= fragment['height']
            row_boundaries.append(cursor_y)
            if row_index % 2:
                document.setFillColorRGB(0.96, 0.96, 0.96)
                document.rect(
                    SESSIONS_TABLE_LEFT,
                    cursor_y,
                    SESSIONS_TABLE_RIGHT - SESSIONS_TABLE_LEFT,
                    row_top - cursor_y,
                    fill=1,
                    stroke=0,
                )
                document.setFillColorRGB(0, 0, 0)

            if fragment['first']:
                serial_number += 1
                document.setFont(FONT_NAME, 8.5)
                _draw_pdf_text(
                    document,
                    SESSIONS_TABLE_SEQUENCE_CENTER,
                    (row_top + cursor_y) / 2 - 3,
                    str(serial_number),
                    8.5,
                    alignment='center',
                )

            for name, _, left, right in SESSIONS_TABLE_COLUMNS:
                lines = fragment['fields'][name]
                font_size = SESSIONS_BODY_FONT_SIZE
                leading = font_size * 1.15
                first_baseline = (
                    (row_top + cursor_y) / 2
                    + (len(lines) - 1) * leading / 2
                )
                document.setFont(FONT_NAME, font_size)
                for line_index, line in enumerate(lines):
                    _draw_pdf_text(
                        document,
                        (left + right) / 2,
                        first_baseline - line_index * leading,
                        line,
                        font_size,
                        alignment='center',
                    )

        while (
            cursor_y - SESSIONS_TABLE_ROW_HEIGHT
            >= SESSIONS_TABLE_BOTTOM
        ):
            row_top = cursor_y
            cursor_y -= SESSIONS_TABLE_ROW_HEIGHT
            row_boundaries.append(cursor_y)
            if (len(row_boundaries) - 1) % 2:
                document.setFillColorRGB(0.96, 0.96, 0.96)
                document.rect(
                    SESSIONS_TABLE_LEFT,
                    cursor_y,
                    SESSIONS_TABLE_RIGHT - SESSIONS_TABLE_LEFT,
                    row_top - cursor_y,
                    fill=1,
                    stroke=0,
                )
                document.setFillColorRGB(0, 0, 0)
            serial_number += 1
            document.setFont(FONT_NAME, 8.5)
            _draw_pdf_text(
                document,
                SESSIONS_TABLE_SEQUENCE_CENTER,
                (row_top + cursor_y) / 2 - 3,
                str(serial_number),
                8.5,
                alignment='center',
            )
        if cursor_y > SESSIONS_TABLE_BOTTOM:
            row_boundaries.append(SESSIONS_TABLE_BOTTOM)

        document.setStrokeColorRGB(0, 0, 0)
        document.setLineWidth(0.5)
        for x in SESSIONS_TABLE_COLUMN_EDGES:
            document.line(
                x,
                SESSIONS_TABLE_BOTTOM,
                x,
                SESSIONS_TABLE_TOP,
            )
        for y in row_boundaries:
            document.line(
                SESSIONS_TABLE_LEFT,
                y,
                SESSIONS_TABLE_RIGHT,
                y,
            )
        document.line(
            SESSIONS_TABLE_LEFT,
            SESSIONS_TABLE_BOTTOM,
            SESSIONS_TABLE_LEFT,
            SESSIONS_TABLE_TOP,
        )
        document.line(
            SESSIONS_TABLE_RIGHT,
            SESSIONS_TABLE_BOTTOM,
            SESSIONS_TABLE_RIGHT,
            SESSIONS_TABLE_TOP,
        )
        document.setFillColorRGB(0, 0, 0)
    document.save()
    output.seek(0)
    return _merge_template_overlay(
        title,
        template,
        output,
        repeat_template_page=True,
    )


def generate_case_pdf(record):
    case_number = record.get('رقم القضية', '')
    return _create_pdf(
        f'تفاصيل القضية {case_number}',
        [
            ('القاضي المختص:', record.get('برئاسة القاضي', '-')),
            ('رقم القضية:', case_number),
            ('تاريخ الورود:', record.get('تاريخ ورودها', '-')),
            ('موضوع القضية:', record.get('موضوعها', '-')),
            ('نوع القضية:', record.get('نوع القضية', '-')),
            ('المدعي:', record.get('المدعي', '-')),
            ('المدعى عليه:', record.get('المدعى عليه', '-')),
            ('آخر قرار للقضية:', record.get('حالة القضية', '-')),
            ('تاريخ الجلسة القادمة:', record.get('الجلسة القادمة', '-')),
        ],
        CASE_INFO_TEMPLATE,
        template_layout='case_info',
    )


def generate_sessions_pdf(records, sitting_date):
    if not records:
        raise CaseRecordsError('لا توجد جلسات لإدراجها في النموذج الرسمي.')
    return _create_sessions_pdf(records, sitting_date)


def generate_attendance_ad_pdf(case_number, plaintiff, defendant, session_date, judge):
    normalized_date = _normalized_date(session_date)
    if normalized_date is None:
        raise CaseRecordsError('تاريخ الجلسة غير صالح لإنشاء الإعلان.')
    return _create_pdf(
        f'إعلان حضور جلسة القضية {case_number}',
        {
            'plaintiff': plaintiff,
            'defendant': defendant,
            'session_date': normalized_date,
            'judge': judge,
        },
        ATTENDANCE_TEMPLATE,
        template_layout='attendance_ad',
    )
