"""
Telegram-бот «Документы за 30 секунд» для самозанятых
Версия 4.0:
  - Юридически значимые документы (ст. ГК РФ, строгая структура)
  - Только ручная оплата (Stars убраны)
  - Распознавание документов через Vision AI (/scan)
  - Профиль пользователя (/profile)
  - История документов (/mydocs)
  - Реферальная система (/ref)
  - Умное приветствие по профессии
  - Аналитика воронки (/funnel)
"""

import logging
import os
import io
import json
import hashlib
import base64
import httpx
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
    CallbackQueryHandler
)

# ══════════════════════════════════════════
#  КОНФИГ
# ══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "milorky")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "2120657855"))
FREE_LIMIT     = 2

# Хранилище в памяти (при рестарте сбрасывается)
user_data: dict = {}

# ══════════════════════════════════════════
#  СОСТОЯНИЯ ДИАЛОГА
# ══════════════════════════════════════════

(
    ONBOARD_PROF,
    PROFILE_1, PROFILE_2, PROFILE_3, PROFILE_4, PROFILE_5,
    CHOOSE_DOC,
    # Сканирование
    SCAN_WAIT_FILE,
    SCAN_CONFIRM,
    SCAN_CHOOSE_DOC,
    # Акт
    ACT_1, ACT_2, ACT_3, ACT_4, ACT_5, ACT_6,
    # Счёт
    INV_1, INV_2, INV_3, INV_4, INV_5,
    # Договор
    CONTRACT_1, CONTRACT_2, CONTRACT_3, CONTRACT_4, CONTRACT_5, CONTRACT_6,
    # Доп. соглашение
    ADD_1, ADD_2, ADD_3, ADD_4,
    # Квитанция
    REC_1, REC_2, REC_3, REC_4,
    # Доверенность
    POA_1, POA_2, POA_3, POA_4,
    # КП
    CP_1, CP_2, CP_3, CP_4, CP_5,
) = range(44)

# ══════════════════════════════════════════
#  ШРИФТЫ
# ══════════════════════════════════════════

FONT_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans-Bold.ttf")

def load_fonts() -> bool:
    try:
        if os.path.exists(FONT_PATH) and os.path.exists(FONT_BOLD_PATH):
            pdfmetrics.registerFont(TTFont('DV',   FONT_PATH))
            pdfmetrics.registerFont(TTFont('DV-B', FONT_BOLD_PATH))
            logger.info("Шрифты загружены")
            return True
        logger.warning("Шрифты не найдены — используется Helvetica (без кириллицы)")
        return False
    except Exception as e:
        logger.warning(f"Ошибка шрифтов: {e}")
        return False

def fn(bold=False) -> str:
    if os.path.exists(FONT_PATH):
        return 'DV-B' if bold else 'DV'
    return 'Helvetica-Bold' if bold else 'Helvetica'

def S(bold=False, size=9, align=TA_LEFT, color='#222222', space=3) -> ParagraphStyle:
    return ParagraphStyle(
        'x', fontName=fn(bold), fontSize=size,
        alignment=align, spaceAfter=space,
        leading=size * 1.45,
        textColor=colors.HexColor(color)
    )

# ══════════════════════════════════════════
#  ХЕЛПЕРЫ ДАННЫХ
# ══════════════════════════════════════════

ACCENT  = '#1a1a2e'
ACCENT2 = '#3a3a6e'
BORDER  = '#cccccc'
BG      = '#f5f5fc'
GRAY    = '#888888'

def get_user(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {
            "profile": {"name":"","phone":"","email":"","inn":"","npd":"","city":""},
            "count":        0,
            "paid":         False,
            "profession":   None,
            "docs":         [],
            "ref_code":     hashlib.md5(str(uid).encode()).hexdigest()[:8].upper(),
            "referred_by":  None,
            "referrals":    0,
            "bonus_docs":   0,
            "funnel_started":     False,
            "funnel_doc_created": False,
            "joined":       datetime.now().strftime('%d.%m.%Y %H:%M'),
            "last_activity":datetime.now().strftime('%d.%m.%Y %H:%M'),
        }
    return user_data[uid]

def prof(uid: int) -> dict:
    return get_user(uid)["profile"]

def touch(uid: int):
    get_user(uid)["last_activity"] = datetime.now().strftime('%d.%m.%Y %H:%M')

def can_gen(uid: int) -> bool:
    u = get_user(uid)
    return u["paid"] or u["bonus_docs"] > 0 or u["count"] < FREE_LIMIT

def is_admin(user) -> bool:
    if ADMIN_ID and user.id == ADMIN_ID:
        return True
    if user.username and user.username.lower() == ADMIN_USERNAME.lower():
        return True
    return False

def docnum() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')

def clean(text: str) -> str:
    """Убирает markdown-мусор из AI-ответа"""
    for ch in ('**','*','##','#','`','---','—'):
        text = text.replace(ch, '')
    return text.strip()

def today() -> str:
    return datetime.now().strftime('%d.%m.%Y')

# ══════════════════════════════════════════
#  ПРОФЕССИИ
# ══════════════════════════════════════════

PROF_DOCS = {
    "🎨 Дизайнер":       ["📄 Акт выполненных работ","💰 Счёт на оплату","📃 Договор оказания услуг","📊 Коммерческое предложение"],
    "💻 Разработчик":    ["📄 Акт выполненных работ","📃 Договор оказания услуг","💰 Счёт на оплату","📝 Доп. соглашение"],
    "✍️ Копирайтер":     ["💰 Счёт на оплату","📄 Акт выполненных работ","📃 Договор оказания услуг","📊 Коммерческое предложение"],
    "📸 Фотограф/видео": ["📃 Договор оказания услуг","📄 Акт выполненных работ","💰 Счёт на оплату","🧾 Квитанция об оплате"],
    "📦 Другой":         ["📄 Акт выполненных работ","💰 Счёт на оплату","📃 Договор оказания услуг",
                          "📝 Доп. соглашение","🧾 Квитанция об оплате","📋 Доверенность","📊 Коммерческое предложение"],
}

DOC_LABELS = {
    "act":"Акт", "invoice":"Счёт", "contract":"Договор",
    "addendum":"Доп_соглашение", "receipt":"Квитанция",
    "poa":"Доверенность", "cp":"Коммерческое_предложение",
}

# Ссылки на нормы ГК РФ для каждого типа документа
DOC_LAW = {
    "act":      "Ст. 720, 753 ГК РФ — приёмка выполненных работ",
    "invoice":  "Ст. 486 ГК РФ — оплата товара (услуги)",
    "contract": "Ст. 779–783 ГК РФ — договор возмездного оказания услуг",
    "addendum": "Ст. 450 ГК РФ — изменение договора по соглашению сторон",
    "receipt":  "Ст. 408 ГК РФ — прекращение обязательства исполнением",
    "poa":      "Ст. 185–189 ГК РФ — доверенность",
    "cp":       "Ст. 435 ГК РФ — оферта",
}

# ══════════════════════════════════════════
#  PDF — ОБЩИЕ БЛОКИ
# ══════════════════════════════════════════

def _header(story, title: str, law_ref: str, subtitle: str = "", num: str = ""):
    W = 17 * cm
    # Тёмная шапка с номером
    story.append(Table(
        [[Paragraph(f"№ {num}  |  {today()}" if num else today(),
                    S(size=8, color='#ffffff', align=TA_RIGHT))]],
        colWidths=[W], rowHeights=[0.7*cm],
        style=TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),colors.HexColor(ACCENT)),
            ('PADDING',(0,0),(-1,-1),8),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ])
    ))
    story.append(Spacer(1, 0.35*cm))
    story.append(Paragraph(title, S(bold=True, size=14, align=TA_CENTER, color=ACCENT, space=3)))
    # Правовое основание
    story.append(Paragraph(law_ref, S(size=8, align=TA_CENTER, color=ACCENT2, space=4)))
    if subtitle:
        story.append(Paragraph(subtitle, S(size=9, align=TA_CENTER, color=GRAY, space=4)))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor(ACCENT2)))
    story.append(Spacer(1, 0.4*cm))

def _parties(story, executor: str, client: str,
             exec_phone="", exec_email="", exec_inn="", exec_npd=""):
    def cell(role, name, phone="", email="", inn="", npd=""):
        items = [
            Paragraph(role, S(bold=True, size=7, color=GRAY, space=1)),
            Paragraph(name, S(bold=True, size=10, color=ACCENT, space=2)),
        ]
        if inn:   items.append(Paragraph(f"ИНН: {inn}",   S(size=8, color='#444')))
        if npd:   items.append(Paragraph(f"№ НПД: {npd}", S(size=8, color='#444')))
        if phone: items.append(Paragraph(f"Тел: {phone}", S(size=8, color='#444')))
        if email: items.append(Paragraph(f"E-mail: {email}", S(size=8, color='#444')))
        return [[i] for i in items]

    tl = Table(cell("ИСПОЛНИТЕЛЬ (Сторона 1)", executor, exec_phone, exec_email, exec_inn, exec_npd),
               colWidths=[7.5*cm], style=TableStyle([('PADDING',(0,0),(-1,-1),5),('VALIGN',(0,0),(-1,-1),'TOP')]))
    tr = Table(cell("ЗАКАЗЧИК (Сторона 2)", client),
               colWidths=[7.5*cm], style=TableStyle([('PADDING',(0,0),(-1,-1),5),('VALIGN',(0,0),(-1,-1),'TOP')]))

    story.append(Table([[tl,tr]], colWidths=[8.5*cm,8.5*cm], style=TableStyle([
        ('BOX',(0,0),(-1,-1),0.5,colors.HexColor(BORDER)),
        ('LINEBEFORE',(1,0),(1,-1),0.5,colors.HexColor(BORDER)),
        ('BACKGROUND',(0,0),(0,-1),colors.HexColor(BG)),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
    ])))
    story.append(Spacer(1,0.4*cm))

def _section(story, title: str, body: str):
    """Раздел с заголовком и телом"""
    items = [
        Paragraph(title, S(bold=True, size=9, color=ACCENT, space=2)),
        HRFlowable(width="100%", thickness=0.3, color=colors.HexColor(BORDER)),
        Spacer(1, 0.1*cm),
    ]
    for line in body.strip().split('\n'):
        if line := clean(line).strip():
            items.append(Paragraph(line, S(size=9, space=3, align=TA_JUSTIFY)))
    items.append(Spacer(1,0.25*cm))
    story.append(KeepTogether(items))

def _total(story, amount: str, label="ИТОГО К ОПЛАТЕ:"):
    story.append(Table(
        [[Paragraph(label, S(bold=True,size=8,color=GRAY)),
          Paragraph(f"{amount} руб.", S(bold=True,size=14,color=ACCENT))]],
        colWidths=[6*cm,11*cm],
        style=TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#eef1fb')),
            ('BOX',(0,0),(-1,-1),1.5,colors.HexColor(ACCENT)),
            ('PADDING',(0,0),(-1,-1),10),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ])
    ))
    story.append(Spacer(1,0.5*cm))

def _signs(story, executor: str, client: str, date=""):
    story.append(Paragraph("ПОДПИСИ СТОРОН:", S(bold=True,size=8,color=GRAY,space=4)))
    story.append(Table([
        [Paragraph("Исполнитель:",S(bold=True,size=8,color=GRAY)),
         Paragraph("Заказчик:",   S(bold=True,size=8,color=GRAY))],
        [Paragraph("_________________________",S(size=9)),
         Paragraph("_________________________",S(size=9))],
        [Paragraph(executor,S(size=8,color='#555')),
         Paragraph(client,  S(size=8,color='#555'))],
        [Paragraph(f"Дата: {date or '____________'}",S(size=8,color=GRAY)),
         Paragraph(f"Дата: {date or '____________'}",S(size=8,color=GRAY))],
        [Paragraph("М.П.",S(size=8,color='#bbb')),
         Paragraph("М.П.",S(size=8,color='#bbb'))],
    ], colWidths=[8.5*cm,8.5*cm], style=TableStyle([
        ('PADDING',(0,0),(-1,-1),6),
        ('BOX',(0,0),(-1,-1),0.3,colors.HexColor(BORDER)),
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#fafafa')),
    ])))

def _footer(story, num=""):
    story.append(Spacer(1,0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#ddd')))
    story.append(Spacer(1,0.2*cm))
    story.append(Table([[
        Paragraph(f"Документ № {num}" if num else "Документ сформирован автоматически",
                  S(size=7,color='#aaa')),
        Paragraph(f"@samozanyat_bot  •  Сгенерировано {today()}",
                  S(size=7,color='#aaa',align=TA_RIGHT)),
    ]], colWidths=[9*cm,8*cm], style=TableStyle([('PADDING',(0,0),(-1,-1),0)])))

def build(story) -> bytes:
    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm
    ).build(story)
    buf.seek(0)
    return buf.read()

# ══════════════════════════════════════════
#  PDF — ГЕНЕРАТОРЫ ДОКУМЕНТОВ
# ══════════════════════════════════════════

def pdf_act(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "АКТ ВЫПОЛНЕННЫХ РАБОТ", DOC_LAW["act"],
            f"г. {f.get('city','')}, {f.get('date','')}", num)
    _parties(story, f.get('executor',''), f.get('client',''),
             f.get('exec_phone',''), f.get('exec_email',''),
             f.get('exec_inn',''), f.get('exec_npd',''))

    _section(story, "1. ПРЕДМЕТ АКТА", ai_text)
    _section(story, "2. РЕЗУЛЬТАТ И КАЧЕСТВО",
        "Работы выполнены в полном объёме, в установленные сроки и надлежащего качества "
        "в соответствии с условиями договора. Заказчик проверил результаты работ "
        "и не имеет претензий к их объёму и качеству.")
    _section(story, "3. СТОИМОСТЬ И НДС",
        f"Общая стоимость выполненных работ составляет {f.get('amount','0')} руб. "
        f"НДС не облагается на основании применения специального налогового режима "
        f"«Налог на профессиональный доход» (Федеральный закон от 27.11.2018 № 422-ФЗ).")

    _total(story, f.get('amount','0'))
    _signs(story, f.get('executor',''), f.get('client',''), f.get('date',''))
    _footer(story, num)
    return build(story)

def pdf_invoice(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "СЧЁТ НА ОПЛАТУ", DOC_LAW["invoice"],
            f"Дата выставления: {f.get('date','')}", num)
    _parties(story, f.get('executor',''), f.get('client',''),
             f.get('exec_phone',''), f.get('exec_email',''),
             f.get('exec_inn',''), f.get('exec_npd',''))

    _section(story, "НАЗНАЧЕНИЕ ПЛАТЕЖА", ai_text)

    # Таблица позиций
    story.append(Paragraph("СОСТАВ СЧЁТА:", S(bold=True,size=8,color=GRAY,space=3)))
    story.append(Table([
        [Paragraph("№",S(bold=True,size=8,color=GRAY)),
         Paragraph("Наименование услуги",S(bold=True,size=8,color=GRAY)),
         Paragraph("Кол-во",S(bold=True,size=8,color=GRAY)),
         Paragraph("Сумма, руб.",S(bold=True,size=8,color=GRAY))],
        [Paragraph("1",S(size=9)),
         Paragraph(f.get('work',''),S(size=9)),
         Paragraph("1",S(size=9)),
         Paragraph(f.get('amount',''),S(bold=True,size=9))],
    ], colWidths=[1*cm,10*cm,2*cm,4*cm], style=TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor(BG)),
        ('BOX',(0,0),(-1,-1),0.5,colors.HexColor(BORDER)),
        ('INNERGRID',(0,0),(-1,-1),0.3,colors.HexColor(BORDER)),
        ('PADDING',(0,0),(-1,-1),6),
        ('FONTNAME',(0,0),(-1,0),fn(True)),
    ])))
    story.append(Spacer(1,0.4*cm))
    _section(story, "УСЛОВИЯ ОПЛАТЫ",
        "НДС не облагается (применяется специальный налоговый режим «НПД», "
        "Федеральный закон от 27.11.2018 № 422-ФЗ). "
        "Оплата в течение 5 (пяти) банковских дней с момента выставления счёта.")
    _total(story, f.get('amount','0'))
    _signs(story, f.get('executor',''), f.get('client',''), f.get('date',''))
    _footer(story, num)
    return build(story)

def pdf_contract(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "ДОГОВОР ВОЗМЕЗДНОГО ОКАЗАНИЯ УСЛУГ", DOC_LAW["contract"],
            f"г. {f.get('city','')}, {f.get('date','')}", num)
    _parties(story, f.get('executor',''), f.get('client',''),
             f.get('exec_phone',''), f.get('exec_email',''),
             f.get('exec_inn',''), f.get('exec_npd',''))

    _section(story, "1. ПРЕДМЕТ ДОГОВОРА",
        f"1.1. Исполнитель обязуется по заданию Заказчика оказать следующие услуги:\n{f.get('work','')}.\n"
        "1.2. Заказчик обязуется принять и оплатить оказанные услуги в порядке и на условиях, "
        "предусмотренных настоящим Договором.")

    _section(story, "2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ",
        f"2.1. Стоимость услуг составляет {f.get('amount','')} руб.\n"
        "2.2. НДС не облагается (режим НПД, Федеральный закон от 27.11.2018 № 422-ФЗ).\n"
        "2.3. Оплата производится в течение 5 (пяти) банковских дней после подписания Акта выполненных работ.")

    _section(story, "3. СРОКИ ОКАЗАНИЯ УСЛУГ",
        f"3.1. Срок оказания услуг: {f.get('deadline','по договорённости сторон')}.\n"
        "3.2. Срок может быть изменён по письменному соглашению Сторон.")

    _section(story, "4. ПРАВА И ОБЯЗАННОСТИ ИСПОЛНИТЕЛЯ",
        "4.1. Исполнитель обязуется оказать услуги надлежащего качества и в установленные сроки.\n"
        "4.2. Исполнитель вправе привлекать третьих лиц только с письменного согласия Заказчика.\n"
        "4.3. Исполнитель обязуется сохранять конфиденциальность полученной информации.")

    _section(story, "5. ПРАВА И ОБЯЗАННОСТИ ЗАКАЗЧИКА",
        "5.1. Заказчик обязуется предоставить всю необходимую информацию и материалы.\n"
        "5.2. Заказчик обязуется принять услуги и подписать Акт в течение 3 (трёх) рабочих дней.\n"
        "5.3. При наличии замечаний Заказчик обязан направить мотивированный отказ в письменной форме.")

    _section(story, "6. ОТВЕТСТВЕННОСТЬ СТОРОН",
        "6.1. За нарушение сроков оплаты Заказчик уплачивает пеню в размере 0,1% в день.\n"
        "6.2. За нарушение сроков оказания услуг Исполнитель уплачивает неустойку 0,1% в день.\n"
        "6.3. Стороны освобождаются от ответственности при наступлении форс-мажора.")

    _section(story, "7. КОНФИДЕНЦИАЛЬНОСТЬ",
        "7.1. Стороны обязуются не разглашать информацию, полученную в ходе исполнения Договора.\n"
        "7.2. Обязательство по конфиденциальности действует 3 (три) года после окончания Договора.")

    _section(story, "8. ПОРЯДОК РАЗРЕШЕНИЯ СПОРОВ",
        "8.1. Споры решаются путём переговоров.\n"
        "8.2. При недостижении соглашения спор передаётся в суд по месту нахождения Истца "
        "в соответствии с законодательством РФ.")

    _section(story, "9. ПРОЧИЕ УСЛОВИЯ", ai_text)

    _section(story, "10. СРОК ДЕЙСТВИЯ И ЗАКЛЮЧИТЕЛЬНЫЕ ПОЛОЖЕНИЯ",
        "10.1. Договор вступает в силу с момента подписания и действует до полного исполнения обязательств.\n"
        "10.2. Договор составлен в двух экземплярах, имеющих равную юридическую силу.")

    _signs(story, f.get('executor',''), f.get('client',''), f.get('date',''))
    _footer(story, num)
    return build(story)

def pdf_addendum(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ", DOC_LAW["addendum"],
            f"№ {num} к Договору № {f.get('contract_num','')} от {f.get('contract_date','')}")
    _parties(story, f.get('executor',''), f.get('client',''),
             exec_inn=f.get('exec_inn',''), exec_npd=f.get('exec_npd',''))

    _section(story, "1. ПРЕДМЕТ СОГЛАШЕНИЯ",
        "Стороны пришли к соглашению внести следующие изменения и дополнения в Договор:")
    _section(story, "2. ВНОСИМЫЕ ИЗМЕНЕНИЯ", ai_text)
    _section(story, "3. ЗАКЛЮЧИТЕЛЬНЫЕ ПОЛОЖЕНИЯ",
        "Настоящее Соглашение является неотъемлемой частью Договора и вступает в силу с момента "
        "подписания обеими Сторонами. Все остальные условия Договора, не затронутые настоящим "
        "Соглашением, остаются без изменений. Соглашение составлено в двух экземплярах.")

    _signs(story, f.get('executor',''), f.get('client',''))
    _footer(story, num)
    return build(story)

def pdf_receipt(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "КВИТАНЦИЯ ОБ ОПЛАТЕ", DOC_LAW["receipt"],
            f"Дата: {f.get('date','')}", num)

    story.append(Table([
        [Paragraph("Получатель:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f.get('executor',''), S(bold=True,size=9,color=ACCENT))],
        [Paragraph("ИНН получателя:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f.get('exec_inn','—'), S(size=9))],
        [Paragraph("Плательщик:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f.get('client',''), S(size=9))],
        [Paragraph("Назначение:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f.get('work',''), S(size=9))],
        [Paragraph("Сумма:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f"{f.get('amount','')} руб.", S(bold=True,size=9,color=ACCENT))],
        [Paragraph("Дата оплаты:", S(bold=True,size=9,color=GRAY)),
         Paragraph(f.get('date',''), S(size=9))],
        [Paragraph("НДС:", S(bold=True,size=9,color=GRAY)),
         Paragraph("Не облагается (НПД, ФЗ № 422-ФЗ)", S(size=9))],
    ], colWidths=[5*cm,12*cm], style=TableStyle([
        ('PADDING',(0,0),(-1,-1),8),
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor(BG),colors.white]),
        ('BOX',(0,0),(-1,-1),0.5,colors.HexColor(BORDER)),
        ('LINEBELOW',(0,0),(-1,-2),0.3,colors.HexColor('#ddd')),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('FONTNAME',(0,0),(0,-1),fn(True)),
    ])))
    story.append(Spacer(1,0.5*cm))
    _total(story, f.get('amount','0'), "СУММА К ПОЛУЧЕНИЮ:")
    story.append(Paragraph("Получатель: _________________________", S(size=9,space=3)))
    story.append(Paragraph(f.get('executor',''), S(size=8,color='#555')))
    _footer(story, num)
    return build(story)

def pdf_poa(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "ДОВЕРЕННОСТЬ", DOC_LAW["poa"],
            f"г. {f.get('city','')}, {f.get('date','')}", num)

    _section(story, "ДОВЕРИТЕЛЬ",
        f"Я, {f.get('grantor','')}, настоящей доверенностью уполномочиваю "
        f"{f.get('attorney','')} (далее — Поверенный) представлять мои интересы "
        "во всех учреждениях, организациях и у физических лиц по вопросам, "
        "указанным в настоящей доверенности.")
    _section(story, "ПОЛНОМОЧИЯ ПОВЕРЕННОГО", ai_text)
    _section(story, "СРОК И УСЛОВИЯ",
        f"Настоящая доверенность выдана сроком на {f.get('validity','')}. "
        "Доверенность выдана без права передоверия.")

    story.append(Spacer(1,0.4*cm))
    story.append(Table([
        [Paragraph("Доверитель:",S(bold=True,size=8,color=GRAY)),
         Paragraph("Поверенный:",S(bold=True,size=8,color=GRAY))],
        [Paragraph("_________________________",S(size=9)),
         Paragraph("_________________________",S(size=9))],
        [Paragraph(f.get('grantor',''),S(size=8,color='#555')),
         Paragraph(f.get('attorney',''),S(size=8,color='#555'))],
        [Paragraph(f"Дата: {f.get('date','')}",S(size=8,color=GRAY)),
         Paragraph("",S(size=8))],
    ], colWidths=[8.5*cm,8.5*cm],
    style=TableStyle([('PADDING',(0,0),(-1,-1),6)])))
    _footer(story, num)
    return build(story)

def pdf_cp(f: dict, ai_text: str) -> bytes:
    story, num = [], docnum()
    _header(story, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ", DOC_LAW["cp"],
            f"Дата: {today()}  |  Действительно до: {f.get('valid_until','')}", num)

    story.append(Table([[
        Paragraph(f"Кому: {f.get('client','')}", S(bold=True,size=11,color=ACCENT)),
        Paragraph(f"От: {f.get('executor','')}", S(size=9,color='#555',align=TA_RIGHT)),
    ]], colWidths=[9*cm,8*cm], style=TableStyle([
        ('PADDING',(0,0),(-1,-1),6),
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor(BG)),
        ('BOX',(0,0),(-1,-1),0.5,colors.HexColor(BORDER)),
    ])))
    story.append(Spacer(1,0.4*cm))

    _section(story, "О НАС", f.get('about',''))

    story.append(Paragraph("МЫ ПРЕДЛАГАЕМ:", S(bold=True,size=8,color=GRAY,space=3)))
    for line in ai_text.strip().split('\n'):
        if c := clean(line).strip():
            story.append(Paragraph(f"✓  {c}", S(size=9,space=4)))
    story.append(Spacer(1,0.4*cm))

    _total(story, f.get('amount',''), "СТОИМОСТЬ УСЛУГ:")
    _section(story, "НАШИ ПРЕИМУЩЕСТВА",
        "• Работаем официально — чек самозанятого после оплаты\n"
        "• Соблюдение сроков и гарантия результата\n"
        "• Бесплатные правки в течение 3 дней после сдачи\n"
        "• Договор и акт — по запросу")

    story.append(Paragraph("КОНТАКТЫ:", S(bold=True,size=8,color=GRAY,space=3)))
    if f.get('exec_phone'): story.append(Paragraph(f"Тел: {f['exec_phone']}", S(size=9)))
    if f.get('exec_email'): story.append(Paragraph(f"E-mail: {f['exec_email']}", S(size=9)))
    _footer(story, num)
    return build(story)

PDF_BUILDERS = {
    "act":pdf_act, "invoice":pdf_invoice, "contract":pdf_contract,
    "addendum":pdf_addendum, "receipt":pdf_receipt, "poa":pdf_poa, "cp":pdf_cp,
}

# ══════════════════════════════════════════
#  AI — ГЕНЕРАЦИЯ ТЕКСТА
# ══════════════════════════════════════════

SYSTEM_LEGAL = (
    "Ты — профессиональный юрист, специализирующийся на документах для самозанятых граждан РФ. "
    "Пишешь тексты для официальных юридических документов. "
    "Требования: строго официальный деловой стиль, юридически грамотно, конкретно. "
    "Никаких markdown-символов, никаких приветствий, никаких пояснений — только текст документа."
)

async def ai_call(prompt: str, system: str = SYSTEM_LEGAL,
                  model: str = "openrouter/auto", max_tokens: int = 700) -> str:
    """Базовый вызов OpenRouter API"""
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role":"system","content":system},
                                   {"role":"user","content":prompt}],
                      "max_tokens": max_tokens,
                      "temperature": 0.2},
            )
            data = r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"AI ошибка ({model}): {e}")
        return ""

async def gen_text(doc_type: str, f: dict) -> str:
    """Генерирует ТОЛЬКО описательную часть документа"""
    ex = f.get('executor','')
    cl = f.get('client','')
    w  = f.get('work','')
    am = f.get('amount','')
    dt = f.get('date','')

    prompts = {
        "act": (
            f"Составь официальное описание выполненной работы для Акта выполненных работ.\n"
            f"Исполнитель: {ex}. Заказчик: {cl}. Работа: {w}. Стоимость: {am} руб. Дата: {dt}.\n"
            f"Требования:\n"
            f"- Только описание работы, без заголовков, без «я», без маркеров.\n"
            f"- Формальный официальный стиль.\n"
            f"- 3-4 предложения.\n"
            f"- Начинай с: «Исполнитель выполнил следующие работы: ...»"
        ),
        "invoice": (
            f"Составь официальное описание услуги для Счёта на оплату.\n"
            f"Исполнитель: {ex}. Заказчик: {cl}. Услуга: {w}. Сумма: {am} руб.\n"
            f"Требования:\n"
            f"- Официальный стиль, без маркеров и заголовков.\n"
            f"- 2-3 предложения с чётким описанием услуги и ожидаемого результата.\n"
            f"- Начинай с: «Настоящий счёт выставляется за ...»"
        ),
        "contract": (
            f"Составь раздел «Прочие условия» для Договора возмездного оказания услуг.\n"
            f"Услуга: {w}. Исполнитель: {ex}. Заказчик: {cl}.\n"
            f"Требования:\n"
            f"- 3-4 предложения об особых условиях, порядке приёмки, правах на результат.\n"
            f"- Официальный юридический стиль.\n"
            f"- Нумеруй как 9.1, 9.2, 9.3"
        ),
        "addendum": (
            f"Составь перечень изменений для Дополнительного соглашения к договору.\n"
            f"Суть изменений: {w}.\n"
            f"Требования:\n"
            f"- 3-5 конкретных пунктов.\n"
            f"- Формат: «1. Пункт X Договора изложить в следующей редакции: ...»\n"
            f"- Официальный юридический стиль."
        ),
        "receipt": (
            f"Составь официальное назначение платежа для Квитанции об оплате.\n"
            f"Плательщик: {cl}. Получатель: {ex}. Услуга: {w}. Сумма: {am} руб.\n"
            f"Требования:\n"
            f"- 1-2 предложения, официально.\n"
            f"- Начинай с: «Оплата по договору возмездного оказания услуг за ...»"
        ),
        "poa": (
            f"Составь перечень полномочий для Доверенности.\n"
            f"Суть полномочий: {w}.\n"
            f"Требования:\n"
            f"- 4-6 конкретных полномочий нумерованным списком.\n"
            f"- Каждое полномочие начинается с инфинитива (подписывать, получать, представлять...).\n"
            f"- Официальный юридический стиль."
        ),
        "cp": (
            f"Составь перечень преимуществ для Коммерческого предложения.\n"
            f"Исполнитель: {ex}. Услуга: {w}.\n"
            f"Требования:\n"
            f"- 5-6 конкретных выгод для клиента.\n"
            f"- Каждая выгода — одно предложение, убедительно и профессионально.\n"
            f"- Без маркеров — каждый пункт с новой строки."
        ),
    }
    result = await ai_call(prompts.get(doc_type, "Напиши текст документа."))
    return result or "Услуги оказаны в полном объёме в соответствии с условиями договора."

# ══════════════════════════════════════════
#  VISION AI — РАСПОЗНАВАНИЕ ДОКУМЕНТОВ
# ══════════════════════════════════════════

# Модели с поддержкой Vision (бесплатные на OpenRouter)
VISION_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "microsoft/phi-3-vision-128k-instruct:free",
    "qwen/qwen-vl-plus:free",
]

VISION_PROMPT = """Извлеки из документа следующие данные:
- Исполнитель (ФИО или название компании, кто выполняет работу)
- Заказчик (ФИО или название компании, кто платит)
- Сумма (только цифры, без знаков валюты)
- Дата (в формате ДД.ММ.ГГГГ)
- Описание услуги или работы (кратко, 1-2 предложения)

Верни ответ СТРОГО в формате JSON, без пояснений, без markdown:
{"executor": "...", "client": "...", "amount": "...", "date": "...", "work": "..."}

Если какое-то поле не найдено — оставь пустую строку."""

async def vision_extract(image_b64: str, mime: str = "image/jpeg") -> dict:
    """Извлекает данные из изображения через Vision AI"""
    for model in VISION_MODELS:
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_KEY}",
                             "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url",
                                 "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                                {"type": "text", "text": VISION_PROMPT},
                            ]
                        }],
                        "max_tokens": 400,
                    }
                )
                text = r.json()["choices"][0]["message"]["content"]
                # Ищем JSON в ответе
                start = text.find('{')
                end   = text.rfind('}') + 1
                if start >= 0 and end > start:
                    return json.loads(text[start:end])
        except Exception as e:
            logger.warning(f"Vision {model} ошибка: {e}")
            continue
    return {}

async def text_extract_ai(text_content: str) -> dict:
    """Извлекает данные из текстового содержимого документа"""
    prompt = (
        f"Из текста документа извлеки данные и верни ТОЛЬКО JSON без пояснений:\n"
        f'{{"executor":"ФИО исполнителя","client":"заказчик","amount":"сумма цифрами",'
        f'"date":"дата ДД.ММ.ГГГГ","work":"описание услуги"}}\n\n'
        f"Если поле не найдено — пустая строка.\n\nТекст:\n{text_content[:3000]}"
    )
    result = await ai_call(prompt, system="Извлекай данные строго по инструкции. Только JSON.")
    try:
        start = result.find('{')
        end   = result.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(result[start:end])
    except Exception as e:
        logger.warning(f"Ошибка парсинга JSON: {e}")
    return {}

# ══════════════════════════════════════════
#  ОТПРАВКА PDF
# ══════════════════════════════════════════

async def send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_type: str):
    """Финальный шаг: генерирует и отправляет PDF"""
    uid = update.effective_user.id
    u   = get_user(uid)

    # Подтягиваем данные профиля если поля пусты
    p = prof(uid)
    for key, pk in [('executor','name'),('exec_phone','phone'),
                    ('exec_email','email'),('exec_inn','inn'),
                    ('exec_npd','npd'),('city','city')]:
        if not context.user_data.get(key) and p.get(pk):
            context.user_data[key] = p[pk]

    await update.message.reply_text("⏳ Генерирую документ...")

    try:
        ai_text   = await gen_text(doc_type, context.user_data)
        pdf_bytes = PDF_BUILDERS[doc_type](context.user_data, ai_text)

        # Счётчики
        if u["bonus_docs"] > 0:
            u["bonus_docs"] -= 1
        else:
            u["count"] += 1
        u["funnel_doc_created"] = True

        name = context.user_data.get('executor','doc').split()[0]
        fname = f"{DOC_LABELS[doc_type]}_{name}_{datetime.now().strftime('%d%m%Y')}.pdf"

        # Сохраняем в историю
        u["docs"].append({
            "type":     doc_type,
            "label":    DOC_LABELS[doc_type].replace('_',' '),
            "filename": fname,
            "date":     datetime.now().strftime('%d.%m.%Y %H:%M'),
            "pdf":      pdf_bytes,
        })

        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=fname,
            caption=f"✅ *{DOC_LABELS[doc_type].replace('_',' ')} готов!*",
            parse_mode='Markdown',
        )

        # Реферальный бонус при первом документе
        if u["count"] == 1 and u.get("referred_by"):
            ref_uid = u["referred_by"]
            if ref_uid in user_data:
                user_data[ref_uid]["referrals"]  += 1
                user_data[ref_uid]["bonus_docs"] += 2
                try:
                    await context.bot.send_message(
                        chat_id=ref_uid,
                        text="🎉 Ваш реферал создал первый документ!\nВам начислено *+2 бесплатных документа*.",
                        parse_mode='Markdown',
                    )
                except Exception:
                    pass

        # Лимит
        if not u["paid"] and u["bonus_docs"] == 0 and u["count"] >= FREE_LIMIT:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Подписка", callback_data="buy"),
                InlineKeyboardButton("👥 Пригласить друга", callback_data="ref"),
            ]])
            await update.message.reply_text(
                "⚠️ *Бесплатный лимит исчерпан.*\n\n"
                "Оформите подписку или пригласите друга (+2 документа).",
                parse_mode='Markdown', reply_markup=kb,
            )
        else:
            left = "∞" if u["paid"] else (u["bonus_docs"] + max(0, FREE_LIMIT - u["count"]))
            await update.message.reply_text(
                f"Ещё документ? /new | История: /mydocs | Осталось: {left}"
            )

    except Exception as e:
        logger.error(f"PDF ошибка: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка генерации. Попробуйте /new")

    return ConversationHandler.END

# ══════════════════════════════════════════
#  СКАНИРОВАНИЕ ДОКУМЕНТОВ (/scan)
# ══════════════════════════════════════════

async def scan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало сканирования — принимаем тип команды"""
    cmd = update.message.text.strip().lower()
    # Сохраняем желаемый тип документа если указан
    if cmd == '/scan_act':
        context.user_data['scan_target'] = 'act'
    elif cmd == '/scan_invoice':
        context.user_data['scan_target'] = 'invoice'
    else:
        context.user_data['scan_target'] = None

    await update.message.reply_text(
        "📎 *Распознавание документа*\n\n"
        "Отправьте фото или PDF документа (акт, счёт, договор, чек).\n"
        "Бот извлечёт данные и поможет создать новый документ.\n\n"
        "/cancel — отмена",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove(),
    )
    return SCAN_WAIT_FILE

async def scan_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем файл и отправляем в Vision AI"""
    uid = update.effective_user.id
    touch(uid)
    msg = await update.message.reply_text("🔍 Анализирую документ...")

    try:
        image_b64 = None
        mime      = "image/jpeg"
        extracted = {}

        # ---- Фото ----
        if update.message.photo:
            photo = update.message.photo[-1]  # берём наибольшее
            file  = await context.bot.get_file(photo.file_id)
            buf   = io.BytesIO()
            await file.download_to_memory(buf)
            buf.seek(0)
            image_b64 = base64.b64encode(buf.read()).decode()
            mime      = "image/jpeg"
            extracted = await vision_extract(image_b64, mime)

        # ---- PDF или другой документ ----
        elif update.message.document:
            doc  = update.message.document
            file = await context.bot.get_file(doc.file_id)
            buf  = io.BytesIO()
            await file.download_to_memory(buf)
            buf.seek(0)
            raw = buf.read()

            if doc.mime_type == 'application/pdf':
                # Пробуем как картинку через base64 (для Vision)
                # Если PDF не поддерживается Vision — парсим текст
                image_b64 = base64.b64encode(raw).decode()
                mime      = "application/pdf"
                extracted = await vision_extract(image_b64, mime)

                # Если Vision не помог — текстовый fallback
                if not extracted or not any(extracted.values()):
                    import re
                    decoded = raw.decode('latin-1', errors='ignore')
                    strings = re.findall(r'[А-Яа-яёЁA-Za-z0-9\s\.,\-\(\)\/\:]{5,}', decoded)
                    text_content = ' '.join(strings[:300])
                    extracted = await text_extract_ai(text_content)
            else:
                # Текстовый файл
                try:
                    text_content = raw.decode('utf-8', errors='ignore')
                except Exception:
                    text_content = raw.decode('cp1251', errors='ignore')
                extracted = await text_extract_ai(text_content)
        else:
            await msg.edit_text("❌ Отправьте фото или PDF документа.")
            return SCAN_WAIT_FILE

        if not extracted or not any(extracted.values()):
            await msg.edit_text(
                "❌ Не удалось распознать документ.\n\n"
                "Попробуйте:\n"
                "• Сфотографировать чётче\n"
                "• Отправить текстовый PDF\n"
                "• Использовать /new для ручного заполнения"
            )
            return ConversationHandler.END

        # Подставляем данные профиля для пустых полей
        p = prof(uid)
        if not extracted.get('executor') and p.get('name'):
            extracted['executor'] = p['name']

        # Сохраняем извлечённые данные
        context.user_data.update({
            'executor':   extracted.get('executor', ''),
            'client':     extracted.get('client',   ''),
            'work':       extracted.get('work',     ''),
            'amount':     extracted.get('amount',   ''),
            'date':       extracted.get('date',     today()),
            'exec_phone': p.get('phone',''),
            'exec_email': p.get('email',''),
            'exec_inn':   p.get('inn',''),
            'exec_npd':   p.get('npd',''),
            'city':       p.get('city',''),
        })
        context.user_data['scan_extracted'] = extracted

        # Показываем результат
        lines = []
        if extracted.get('executor'): lines.append(f"👤 Исполнитель: {extracted['executor']}")
        if extracted.get('client'):   lines.append(f"🏢 Заказчик: {extracted['client']}")
        if extracted.get('work'):     lines.append(f"📋 Услуга: {extracted['work'][:80]}")
        if extracted.get('amount'):   lines.append(f"💰 Сумма: {extracted['amount']} руб.")
        if extracted.get('date'):     lines.append(f"📅 Дата: {extracted['date']}")

        summary = '\n'.join(lines) if lines else "Данные извлечены частично"

        await msg.edit_text(
            f"✅ *Данные из документа:*\n\n{summary}\n\n"
            "Всё верно?",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(
                [["✅ Да, создать документ", "❌ Нет, ввести вручную"]],
                resize_keyboard=True, one_time_keyboard=True,
            )
        )
        return SCAN_CONFIRM

    except Exception as e:
        logger.error(f"Scan ошибка: {e}", exc_info=True)
        await msg.edit_text("❌ Ошибка обработки. Попробуйте /new")
        return ConversationHandler.END

async def scan_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь подтверждает данные"""
    text = update.message.text

    if "Нет" in text or "❌" in text:
        await update.message.reply_text(
            "Хорошо, введите данные вручную. /new",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # Если есть заранее заданный тип документа
    target = context.user_data.get('scan_target')
    if target and target in PDF_BUILDERS:
        context.user_data['doc_type'] = target
        return await send_pdf(update, context, target)

    # Иначе — предлагаем выбрать тип
    uid  = update.effective_user.id
    u    = get_user(uid)
    prof_key = u.get("profession", "📦 Другой")
    docs = PROF_DOCS.get(prof_key, PROF_DOCS["📦 Другой"])
    rows = [docs[i:i+2] for i in range(0, len(docs), 2)]

    await update.message.reply_text(
        "Выберите тип документа для создания:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True),
    )
    return SCAN_CHOOSE_DOC

async def scan_choose_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа после сканирования"""
    text = update.message.text
    mapping = {
        "Акт":"act", "Счёт":"invoice", "Договор":"contract",
        "Доп":"addendum", "Квитанция":"receipt", "Доверенность":"poa", "Коммерческое":"cp",
    }
    doc_type = next((v for k,v in mapping.items() if k in text), None)
    if not doc_type:
        await update.message.reply_text("Выберите из меню.")
        return SCAN_CHOOSE_DOC

    context.user_data['doc_type'] = doc_type
    return await send_pdf(update, context, doc_type)

# ══════════════════════════════════════════
#  ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ
# ══════════════════════════════════════════

async def profile_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p   = prof(uid)
    touch(uid)
    filled = sum(1 for v in p.values() if v)
    await update.message.reply_text(
        f"👤 *Ваш профиль* ({filled}/{len(p)} заполнено)\n\n"
        f"ФИО: {p.get('name') or '—'}\n"
        f"Телефон: {p.get('phone') or '—'}\n"
        f"Email: {p.get('email') or '—'}\n"
        f"ИНН: {p.get('inn') or '—'}\n"
        f"№ НПД: {p.get('npd') or '—'}\n"
        f"Город: {p.get('city') or '—'}\n\n"
        "_Данные профиля автоматически подставляются в документы_",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Заполнить профиль", callback_data="edit_profile")
        ]])
    )

async def profile_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "✏️ *Настройка профиля*\n\n*Шаг 1/5* — Ваше ФИО\n\nПример: Иванов Иван Иванович",
        parse_mode='Markdown', reply_markup=ReplyKeyboardRemove(),
    )
    return PROFILE_1

async def p1(u, c): prof(u.effective_user.id)['name']  = u.message.text; await u.message.reply_text("*Шаг 2/5* — Телефон _(или «-»)_", parse_mode='Markdown'); return PROFILE_2
async def p2(u, c): prof(u.effective_user.id)['phone'] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 3/5* — Email _(или «-»)_", parse_mode='Markdown'); return PROFILE_3
async def p3(u, c): prof(u.effective_user.id)['email'] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 4/5* — ИНН (12 цифр) _(или «-»)_", parse_mode='Markdown'); return PROFILE_4
async def p4(u, c): prof(u.effective_user.id)['inn']   = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 5/5* — Город и № НПД\n\nПример: Москва, 123456789\n_(НПД можно «-»)_", parse_mode='Markdown'); return PROFILE_5
async def p5(u, c):
    parts = [x.strip() for x in u.message.text.split(",")]
    pr = prof(u.effective_user.id)
    pr['city'] = parts[0]
    pr['npd']  = parts[1] if len(parts)>1 and parts[1]!="-" else ""
    await u.message.reply_text(
        "✅ *Профиль сохранён!*\n\nДанные автоматически подставляются в документы.\n\n/new — создать документ",
        parse_mode='Markdown',
    )
    return ConversationHandler.END

# ══════════════════════════════════════════
#  ИСТОРИЯ ДОКУМЕНТОВ
# ══════════════════════════════════════════

async def mydocs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    docs = get_user(uid).get("docs", [])
    touch(uid)
    if not docs:
        await update.message.reply_text("📂 Документов пока нет.\n\n/new — создать первый")
        return
    await update.message.reply_text(f"📂 *Ваши документы ({len(docs)} шт.):*", parse_mode='Markdown')
    for i, doc in enumerate(reversed(docs[-10:])):
        real_i = len(docs) - 1 - i
        await update.message.reply_text(
            f"*{doc['label']}*\n🕐 {doc['date']}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Скачать", callback_data=f"dl_{uid}_{real_i}"),
                InlineKeyboardButton("🗑 Удалить",  callback_data=f"rm_{uid}_{real_i}"),
            ]])
        )

async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, uid_s, idx_s = q.data.split("_", 2)
    uid, idx = int(uid_s), int(idx_s)
    if q.from_user.id != uid:
        await q.answer("Это чужой документ!", show_alert=True); return
    docs = user_data.get(uid, {}).get("docs", [])
    if idx >= len(docs):
        await q.answer("Не найден.", show_alert=True); return
    doc = docs[idx]
    await q.message.reply_document(
        document=io.BytesIO(doc["pdf"]), filename=doc["filename"],
        caption=f"📄 {doc['label']} от {doc['date']}",
    )

async def cb_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, uid_s, idx_s = q.data.split("_", 2)
    uid, idx = int(uid_s), int(idx_s)
    if q.from_user.id != uid:
        await q.answer("Это чужой документ!", show_alert=True); return
    docs = user_data.get(uid, {}).get("docs", [])
    if idx < len(docs):
        docs.pop(idx)
        await q.message.edit_text("🗑 Удалено.")

# ══════════════════════════════════════════
#  РЕФЕРАЛЫ
# ══════════════════════════════════════════

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u   = get_user(uid)
    touch(uid)
    info  = await context.bot.get_me()
    link  = f"https://t.me/{info.username}?start=ref_{u['ref_code']}"
    await update.message.reply_text(
        f"👥 *Реферальная программа*\n\n"
        f"За каждого друга, который создаст первый документ:\n"
        f"• Вам начисляется *+2 бесплатных документа*\n\n"
        f"🔗 Ваша ссылка:\n`{link}`\n\n"
        f"Приглашено: *{u.get('referrals',0)}*  |  Бонусных: *{u.get('bonus_docs',0)}*",
        parse_mode='Markdown',
    )

# ══════════════════════════════════════════
#  АНАЛИТИКА / АДМИН
# ══════════════════════════════════════════

async def funnel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён."); return
    total   = len(user_data)
    started = sum(1 for u in user_data.values() if u.get("funnel_started"))
    created = sum(1 for u in user_data.values() if u.get("funnel_doc_created"))
    paid_n  = sum(1 for u in user_data.values() if u.get("paid"))
    t_docs  = sum(u.get("count",0) for u in user_data.values())
    refs    = sum(u.get("referrals",0) for u in user_data.values())
    def pct(a,b): return f"{a/b*100:.1f}%" if b else "—"
    pc: dict = {}
    for u in user_data.values():
        p = u.get("profession") or "не указана"
        pc[p] = pc.get(p,0) + 1
    prof_lines = "\n".join(f"  {p}: {c}" for p,c in sorted(pc.items(),key=lambda x:-x[1]))
    await update.message.reply_text(
        f"📊 *Воронка*\n\n"
        f"👥 Всего: {total}\n"
        f"🚀 /new: {started} ({pct(started,total)})\n"
        f"📄 Документов: {created} ({pct(created,started)})\n"
        f"💳 Оплатили: {paid_n} ({pct(paid_n,created)})\n\n"
        f"📑 Всего docs: {t_docs}\n"
        f"👫 Рефералов: {refs}\n\n"
        f"*Профессии:*\n{prof_lines}",
        parse_mode='Markdown',
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён."); return
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 {len(user_data)} пользователей\n"
        f"📄 {sum(u.get('count',0) for u in user_data.values())} документов\n"
        f"💳 {sum(1 for u in user_data.values() if u.get('paid'))} подписок",
        parse_mode='Markdown',
    )

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён."); return
    msg = "📋 *Последние пользователи:*\n\n"
    for uid, u in list(user_data.items())[-10:]:
        msg += f"• `{uid}` {u.get('profession','?')} | docs:{u.get('count',0)} | {'✅' if u.get('paid') else '❌'}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not context.args:
        await update.message.reply_text("Укажи ID: /user 123456789"); return
    try:
        uid  = int(context.args[0])
        data = user_data.get(uid)
        if not data:
            await update.message.reply_text("Не найден."); return
        pr = data.get("profile", {})
        await update.message.reply_text(
            f"🔍 `{uid}`\n"
            f"ФИО: {pr.get('name','?')}\n"
            f"Профессия: {data.get('profession','?')}\n"
            f"Docs: {data.get('count',0)} | Paid: {'✅' if data.get('paid') else '❌'}\n"
            f"Рефералов: {data.get('referrals',0)} | Бонус: {data.get('bonus_docs',0)}\n"
            f"Активность: {data.get('last_activity','?')}",
            parse_mode='Markdown',
        )
    except ValueError:
        await update.message.reply_text("ID — число")

# ══════════════════════════════════════════
#  ОПЛАТА (только ручная)
# ══════════════════════════════════════════

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Написать @milorky", url="https://t.me/milorky")
    ]])
    text = (
        "💳 *Подписка — 299 ₽/месяц*\n\n"
        "✅ Безлимитные документы\n"
        "✅ Все 7 типов документов\n"
        "✅ PDF с профессиональным дизайном\n"
        "✅ История документов\n\n"
        "Как оплатить:\n"
        "1. Напишите @milorky\n"
        "2. Я пришлю реквизиты для оплаты\n"
        "3. После оплаты активирую подписку вручную\n\n"
        "Или позовите друга и получите *2 бесплатных документа* — /ref"
    )
    msg = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.answer()
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=kb)

async def activate_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    try:
        tid = int(context.args[0])
        get_user(tid)["paid"] = True
        await update.message.reply_text(f"✅ Подписка активирована для {tid}")
        await context.bot.send_message(
            chat_id=tid,
            text="🎉 *Подписка активирована!* Документы без ограничений. /new",
            parse_mode='Markdown',
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш ID: `{update.effective_user.id}`\n\nОтправьте @milorky после оплаты.",
        parse_mode='Markdown',
    )

# ══════════════════════════════════════════
#  ОНБОРДИНГ И ГЛАВНОЕ МЕНЮ
# ══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    u    = get_user(uid)
    touch(uid)

    # Реф-ссылка
    for arg in (context.args or []):
        if arg.startswith("ref_") and not u.get("referred_by"):
            code = arg[4:]
            for other_uid, other in user_data.items():
                if other.get("ref_code") == code and other_uid != uid:
                    u["referred_by"] = other_uid
                    break

    if u.get("profession"):
        await _main_menu(update, u)
        return ConversationHandler.END

    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Создаю профессиональные юридически значимые документы для самозанятых за 30 секунд.\n\n"
        "Кем вы работаете? Покажу нужные документы 👇",
        reply_markup=ReplyKeyboardMarkup(
            [["🎨 Дизайнер", "💻 Разработчик"],
             ["✍️ Копирайтер", "📸 Фотограф/видео"],
             ["📦 Другой"]],
            resize_keyboard=True, one_time_keyboard=True,
        )
    )
    return ONBOARD_PROF

async def onboard_prof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text
    u    = get_user(uid)
    u["profession"] = text if text in PROF_DOCS else "📦 Другой"
    touch(uid)
    await _main_menu(update, u, welcome=True)
    return ConversationHandler.END

async def _main_menu(update: Update, u: dict, welcome=False):
    prof_key = u.get("profession","📦 Другой")
    docs     = PROF_DOCS.get(prof_key, PROF_DOCS["📦 Другой"])
    status   = "✅ Безлимит" if u["paid"] else f"🆓 Осталось: {u['bonus_docs']+max(0,FREE_LIMIT-u['count'])}"
    prefix   = f"🎉 Для *{prof_key}* — ваши документы:\n\n" if welcome else f"📂 Документы ({prof_key}):\n\n"
    await update.message.reply_text(
        prefix + "\n".join(f"• {d}" for d in docs) +
        f"\n\n{status}\n\n"
        "/new — создать документ\n"
        "/scan — распознать из фото/PDF\n"
        "/profile — мой профиль\n"
        "/mydocs — история\n"
        "/ref — пригласить друга\n"
        "/buy — подписка",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove(),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Как пользоваться:*\n\n"
        "1. /new — создать документ вручную\n"
        "2. /scan — отправить фото/PDF, бот заполнит сам\n"
        "3. /profile — заполнить профиль (данные подставятся автоматически)\n"
        "4. /mydocs — история документов\n"
        "5. /ref — пригласить друга (+2 документа)\n\n"
        "Если поле не нужно — напишите «-»\n\n"
        "Поддержка: @milorky",
        parse_mode='Markdown',
    )

# ══════════════════════════════════════════
#  СОЗДАНИЕ ДОКУМЕНТОВ (/new)
# ══════════════════════════════════════════

async def new_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u   = get_user(uid)
    touch(uid)
    u["funnel_started"] = True

    if not can_gen(uid):
        await update.message.reply_text(
            "⚠️ *Лимит исчерпан*\n\nОформите подписку или пригласите друга (+2 документа).",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Подписка", callback_data="buy"),
                InlineKeyboardButton("👥 Пригласить", callback_data="ref"),
            ]])
        )
        return ConversationHandler.END

    prof_key = u.get("profession","📦 Другой")
    docs     = PROF_DOCS.get(prof_key, PROF_DOCS["📦 Другой"])
    rows     = [docs[i:i+2] for i in range(0, len(docs), 2)]
    await update.message.reply_text(
        "📂 *Выберите документ:*",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True),
    )
    return CHOOSE_DOC

async def choose_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = update.message.text
    uid  = update.effective_user.id
    rm   = ReplyKeyboardRemove()

    # Подтягиваем профиль в user_data
    p = prof(uid)
    context.user_data.update({
        'executor':   p.get('name',''),
        'exec_phone': p.get('phone',''),
        'exec_email': p.get('email',''),
        'exec_inn':   p.get('inn',''),
        'exec_npd':   p.get('npd',''),
        'city':       p.get('city',''),
    })

    mapping = {
        "Акт":          ("act",      "📄 Акт выполненных работ",       6, ACT_1),
        "Счёт":         ("invoice",  "💰 Счёт на оплату",              5, INV_1),
        "Договор":      ("contract", "📃 Договор оказания услуг",      6, CONTRACT_1),
        "Доп":          ("addendum", "📝 Доп. соглашение",             4, ADD_1),
        "Квитанция":    ("receipt",  "🧾 Квитанция об оплате",         4, REC_1),
        "Доверенность": ("poa",      "📋 Доверенность",                4, POA_1),
        "Коммерческое": ("cp",       "📊 Коммерческое предложение",    5, CP_1),
    }

    for kw, (dtype, label, steps, state) in mapping.items():
        if kw in text:
            context.user_data["doc_type"] = dtype
            hint = f"\n✅ Профиль: *{p['name']}* (Enter = подтвердить)" if p.get('name') else ""
            await update.message.reply_text(
                f"{label}\n\n*Шаг 1/{steps}* — Ваше ФИО{hint}",
                parse_mode='Markdown', reply_markup=rm,
            )
            return state

    await update.message.reply_text("Выберите из меню.")
    return CHOOSE_DOC

# ══════════════════════════════════════════
#  ШАГИ ДИАЛОГОВ
# ══════════════════════════════════════════

def _use_if_set(new_val: str, existing: str) -> str:
    """Если пользователь ввёл пустое/пробел — оставляем старое значение из профиля"""
    return new_val.strip() if new_val.strip() and new_val.strip() != "-" else existing

# ---- АКТ (6 шагов) ----
async def act_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/6* — Название заказчика", parse_mode='Markdown'); return ACT_2
async def act_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/6* — Что сделали?\n\nПример: Разработка сайта-визитки", parse_mode='Markdown'); return ACT_3
async def act_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/6* — Сумма (руб.)\n\nПример: 15000", parse_mode='Markdown'); return ACT_4
async def act_4(u, c):
    c.user_data["amount"] = u.message.text
    await u.message.reply_text("*Шаг 5/6* — Дата\n\nПример: 27 июня 2026  _(«-» = сегодня)_", parse_mode='Markdown'); return ACT_5
async def act_5(u, c):
    c.user_data["date"] = today() if u.message.text=="-" else u.message.text
    inn_hint = f"\n_(в профиле: {c.user_data.get('exec_inn')})_" if c.user_data.get('exec_inn') else ""
    await u.message.reply_text(f"*Шаг 6/6* — Ваш ИНН{inn_hint}\n_(«-» = пропустить)_", parse_mode='Markdown'); return ACT_6
async def act_6(u, c):
    if u.message.text != "-": c.user_data["exec_inn"] = u.message.text
    return await send_pdf(u, c, "act")

# ---- СЧЁТ (5 шагов) ----
async def inv_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/5* — Название заказчика", parse_mode='Markdown'); return INV_2
async def inv_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/5* — Услуга\n\nПример: Дизайн логотипа", parse_mode='Markdown'); return INV_3
async def inv_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/5* — Сумма (руб.)\n\nПример: 25000", parse_mode='Markdown'); return INV_4
async def inv_4(u, c):
    c.user_data["amount"] = u.message.text
    await u.message.reply_text("*Шаг 5/5* — Дата  _(«-» = сегодня)_", parse_mode='Markdown'); return INV_5
async def inv_5(u, c):
    c.user_data["date"] = today() if u.message.text=="-" else u.message.text
    return await send_pdf(u, c, "invoice")

# ---- ДОГОВОР (6 шагов) ----
async def con_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/6* — Название заказчика", parse_mode='Markdown'); return CONTRACT_2
async def con_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/6* — Предмет договора (что делаете?)", parse_mode='Markdown'); return CONTRACT_3
async def con_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/6* — Стоимость (руб.)\n\nПример: 30000", parse_mode='Markdown'); return CONTRACT_4
async def con_4(u, c):
    c.user_data["amount"] = u.message.text
    await u.message.reply_text("*Шаг 5/6* — Срок исполнения\n\nПример: 14 дней", parse_mode='Markdown'); return CONTRACT_5
async def con_5(u, c):
    c.user_data["deadline"] = u.message.text
    city = c.user_data.get("city","")
    hint = f"\n_(в профиле: {city}, «-» = использовать его)_" if city else ""
    await u.message.reply_text(f"*Шаг 6/6* — Город и дата\n\nПример: Москва, 27 июня 2026{hint}", parse_mode='Markdown'); return CONTRACT_6
async def con_6(u, c):
    if u.message.text != "-":
        parts = [x.strip() for x in u.message.text.split(",")]
        c.user_data["city"] = parts[0]
        c.user_data["date"] = parts[1] if len(parts)>1 else today()
    else:
        c.user_data["date"] = today()
    return await send_pdf(u, c, "contract")

# ---- ДОП. СОГЛАШЕНИЕ (4 шага) ----
async def add_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/4* — Название заказчика", parse_mode='Markdown'); return ADD_2
async def add_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/4* — Номер и дата основного договора\n\nПример: 12, 15 января 2026", parse_mode='Markdown'); return ADD_3
async def add_3(u, c):
    parts = [x.strip() for x in u.message.text.split(",")]
    c.user_data["contract_num"]  = parts[0]
    c.user_data["contract_date"] = parts[1] if len(parts)>1 else ""
    await u.message.reply_text("*Шаг 4/4* — Суть изменений\n\nПример: увеличение суммы до 50000 руб., продление срока на 2 недели", parse_mode='Markdown'); return ADD_4
async def add_4(u, c):
    c.user_data["work"] = u.message.text
    return await send_pdf(u, c, "addendum")

# ---- КВИТАНЦИЯ (4 шага) ----
async def rec_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/4* — Плательщик (кто платит)", parse_mode='Markdown'); return REC_2
async def rec_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/4* — За что оплата?\n\nПример: Разработка логотипа", parse_mode='Markdown'); return REC_3
async def rec_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/4* — Сумма и дата\n\nПример: 10000, 27 июня 2026", parse_mode='Markdown'); return REC_4
async def rec_4(u, c):
    parts = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = parts[0]
    c.user_data["date"]   = parts[1] if len(parts)>1 else today()
    return await send_pdf(u, c, "receipt")

# ---- ДОВЕРЕННОСТЬ (4 шага) ----
async def poa_1(u, c):
    c.user_data["grantor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/4* — ФИО поверенного (кому доверяете)", parse_mode='Markdown'); return POA_2
async def poa_2(u, c):
    c.user_data["attorney"] = u.message.text
    await u.message.reply_text("*Шаг 3/4* — Суть полномочий\n\nПример: подписание договоров, получение оплаты", parse_mode='Markdown'); return POA_3
async def poa_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/4* — Срок и город\n\nПример: 1 год, Москва", parse_mode='Markdown'); return POA_4
async def poa_4(u, c):
    parts = [x.strip() for x in u.message.text.split(",")]
    c.user_data["validity"] = parts[0]
    c.user_data["city"]     = parts[1] if len(parts)>1 else c.user_data.get("city","")
    c.user_data["date"]     = today()
    return await send_pdf(u, c, "poa")

# ---- КП (5 шагов) ----
async def cp_1(u, c):
    c.user_data["executor"] = _use_if_set(u.message.text, c.user_data.get("executor",""))
    await u.message.reply_text("*Шаг 2/5* — Кому КП?\n\nПример: ООО «Ромашка»", parse_mode='Markdown'); return CP_2
async def cp_2(u, c):
    c.user_data["client"] = u.message.text
    await u.message.reply_text("*Шаг 3/5* — Что предлагаете?\n\nПример: Разработка сайта под ключ", parse_mode='Markdown'); return CP_3
async def cp_3(u, c):
    c.user_data["work"] = u.message.text
    await u.message.reply_text("*Шаг 4/5* — Стоимость (руб.)\n\nПример: 80000", parse_mode='Markdown'); return CP_4
async def cp_4(u, c):
    c.user_data["amount"] = u.message.text
    await u.message.reply_text("*Шаг 5/5* — Кратко о себе\n\nПример: 5 лет опыта, 200+ проектов", parse_mode='Markdown'); return CP_5
async def cp_5(u, c):
    c.user_data["about"]       = u.message.text
    c.user_data["date"]        = today()
    c.user_data["valid_until"] = "30 дней"
    return await send_pdf(u, c, "cp")

# ══════════════════════════════════════════
#  РАЗНОЕ
# ══════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /new — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    if data == "buy":
        await buy(update, context)
    elif data == "ref":
        await q.answer()
        await ref(update, context)
    elif data == "edit_profile":
        return await profile_edit_start(update, context)
    elif data.startswith("dl_"):
        await cb_download(update, context)
    elif data.startswith("rm_"):
        await cb_remove(update, context)
    else:
        await q.answer()

# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    load_fonts()

    app = Application.builder().token(BOT_TOKEN).build()

    # Онбординг
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ONBOARD_PROF: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_prof)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Профиль
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_show),
            CallbackQueryHandler(profile_edit_start, pattern="^edit_profile$"),
        ],
        states={
            PROFILE_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, p1)],
            PROFILE_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, p2)],
            PROFILE_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, p3)],
            PROFILE_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, p4)],
            PROFILE_5:[MessageHandler(filters.TEXT & ~filters.COMMAND, p5)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Сканирование документов
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("scan",         scan_start),
            CommandHandler("scan_act",     scan_start),
            CommandHandler("scan_invoice", scan_start),
        ],
        states={
            SCAN_WAIT_FILE: [
                MessageHandler(filters.PHOTO,        scan_receive_file),
                MessageHandler(filters.Document.ALL, scan_receive_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, scan_receive_file),
            ],
            SCAN_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scan_confirm),
            ],
            SCAN_CHOOSE_DOC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scan_choose_doc),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Создание документов
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("new", new_doc)],
        states={
            CHOOSE_DOC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_doc)],
            ACT_1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_1)],
            ACT_2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_2)],
            ACT_3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_3)],
            ACT_4:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_4)],
            ACT_5:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_5)],
            ACT_6:       [MessageHandler(filters.TEXT & ~filters.COMMAND, act_6)],
            INV_1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_1)],
            INV_2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_2)],
            INV_3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_3)],
            INV_4:       [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_4)],
            INV_5:       [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_5)],
            CONTRACT_1:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_1)],
            CONTRACT_2:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_2)],
            CONTRACT_3:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_3)],
            CONTRACT_4:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_4)],
            CONTRACT_5:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_5)],
            CONTRACT_6:  [MessageHandler(filters.TEXT & ~filters.COMMAND, con_6)],
            ADD_1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_1)],
            ADD_2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_2)],
            ADD_3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_3)],
            ADD_4:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_4)],
            REC_1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_1)],
            REC_2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_2)],
            REC_3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_3)],
            REC_4:       [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_4)],
            POA_1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_1)],
            POA_2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_2)],
            POA_3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_3)],
            POA_4:       [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_4)],
            CP_1:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_1)],
            CP_2:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_2)],
            CP_3:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_3)],
            CP_4:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_4)],
            CP_5:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_5)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Прочие команды
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("buy",         buy))
    app.add_handler(CommandHandler("myid",        my_id))
    app.add_handler(CommandHandler("mydocs",      mydocs))
    app.add_handler(CommandHandler("ref",         ref))
    app.add_handler(CommandHandler("activate_id", activate_id))
    app.add_handler(CommandHandler("stats",       stats))
    app.add_handler(CommandHandler("users",       users_cmd))
    app.add_handler(CommandHandler("user",        user_info))
    app.add_handler(CommandHandler("funnel",      funnel))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Бот v4.0 запущен ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
