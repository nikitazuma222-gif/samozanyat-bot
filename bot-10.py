"""
Telegram-бот «Документы за 30 секунд» для самозанятых
Версия 2.0 — 5 новых фич:
  1. Умное приветствие по профессии (конверсия)
  2. История документов /mydocs (личный кабинет)
  3. Аналитика воронки /funnel (для админа)
  4. Автоматическая оплата через Telegram Stars
  5. Реферальная система с промокодами
"""

import logging
import os
import io
import hashlib
import httpx
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler
)

# ====== КОНФИГ ======

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.getenv("BOT_TOKEN")
OPENROUTER_KEY  = os.getenv("OPENROUTER_KEY")
ADMIN_USERNAME  = os.getenv("ADMIN_USERNAME", "milorky")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "2120657855"))

# Цена подписки в Stars (1 Star ≈ 0.013$, 299 руб ≈ ~350 Stars)
SUBSCRIPTION_STARS = 350
FREE_LIMIT = 2

# Хранилище в памяти (при рестарте сбрасывается — для персистентности замени на SQLite/Redis)
user_data: dict = {}

# ====== СОСТОЯНИЯ ДИАЛОГА ======

(
    ONBOARD_PROF,      # новый: выбор профессии при онбординге
    CHOOSE_DOC,
    ACT_1, ACT_2, ACT_3, ACT_4, ACT_5, ACT_6,
    INV_1, INV_2, INV_3, INV_4, INV_5,
    CONTRACT_1, CONTRACT_2, CONTRACT_3, CONTRACT_4, CONTRACT_5, CONTRACT_6,
    ADDENDUM_1, ADDENDUM_2, ADDENDUM_3, ADDENDUM_4,
    RECEIPT_1, RECEIPT_2, RECEIPT_3, RECEIPT_4,
    POA_1, POA_2, POA_3, POA_4,
    CP_1, CP_2, CP_3, CP_4, CP_5,
) = range(36)

# ====== ШРИФТЫ ======

FONT_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans-Bold.ttf")

def download_fonts() -> bool:
    try:
        if os.path.exists(FONT_PATH) and os.path.exists(FONT_BOLD_PATH):
            pdfmetrics.registerFont(TTFont('DV',   FONT_PATH))
            pdfmetrics.registerFont(TTFont('DV-B', FONT_BOLD_PATH))
            logger.info("Шрифты загружены успешно")
            return True
        logger.warning("Файлы шрифтов не найдены — используется Helvetica")
        return False
    except Exception as e:
        logger.warning(f"Ошибка загрузки шрифтов: {e}")
        return False

def S(bold=False, size=9, align=TA_LEFT, color='#222222', space=3) -> ParagraphStyle:
    """Фабрика стилей для ReportLab"""
    fn = ('DV-B' if bold else 'DV') if os.path.exists(FONT_PATH) else ('Helvetica-Bold' if bold else 'Helvetica')
    return ParagraphStyle('x', fontName=fn, fontSize=size,
                          alignment=align, spaceAfter=space,
                          leading=size * 1.4, textColor=colors.HexColor(color))

# ====== ХЕЛПЕРЫ ДАННЫХ ======

def get_user(uid: int) -> dict:
    """Возвращает запись пользователя, создаёт если нет"""
    if uid not in user_data:
        user_data[uid] = {
            "count": 0,
            "paid": False,
            "profession": None,
            "docs": [],           # история документов
            "ref_code": _make_ref(uid),
            "referred_by": None,
            "referrals": 0,       # кол-во приглашённых
            "bonus_docs": 0,      # бонусные документы за рефералов
            "joined": datetime.now().strftime('%d.%m.%Y %H:%M'),
            "last_activity": datetime.now().strftime('%d.%m.%Y %H:%M'),
            # аналитика воронки
            "funnel_started": False,   # нажал /new
            "funnel_doc_created": False,
            "funnel_paid": False,
        }
    return user_data[uid]

def _make_ref(uid: int) -> str:
    """Генерирует короткий уникальный реф-код из user id"""
    return hashlib.md5(str(uid).encode()).hexdigest()[:8].upper()

def can_generate(uid: int) -> bool:
    u = get_user(uid)
    if u["paid"] or u["bonus_docs"] > 0:
        return True
    return u["count"] < FREE_LIMIT

def touch(uid: int):
    """Обновляет время последней активности"""
    get_user(uid)["last_activity"] = datetime.now().strftime('%d.%m.%Y %H:%M')

def is_admin(user) -> bool:
    if ADMIN_ID and user.id == ADMIN_ID:
        return True
    if user.username and user.username.lower() == ADMIN_USERNAME.lower():
        return True
    return False

def doc_number() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M')

# ====== ПРОФЕССИИ → ДОКУМЕНТЫ ======

# Маппинг профессии → приоритетные типы документов (показываются первыми)
PROF_DOCS = {
    "🎨 Дизайнер":       ["📄 Акт выполненных работ", "💰 Счёт на оплату", "📃 Договор оказания услуг", "📊 Коммерческое предложение"],
    "💻 Разработчик":    ["📄 Акт выполненных работ", "📃 Договор оказания услуг", "💰 Счёт на оплату", "📝 Доп. соглашение"],
    "✍️ Копирайтер":     ["💰 Счёт на оплату", "📄 Акт выполненных работ", "📃 Договор оказания услуг", "📊 Коммерческое предложение"],
    "📸 Фотограф/видео": ["📃 Договор оказания услуг", "📄 Акт выполненных работ", "💰 Счёт на оплату", "🧾 Квитанция об оплате"],
    "📦 Другой":         ["📄 Акт выполненных работ", "💰 Счёт на оплату", "📃 Договор оказания услуг",
                          "📝 Доп. соглашение", "🧾 Квитанция об оплате", "📋 Доверенность", "📊 Коммерческое предложение"],
}

DOC_LABELS = {
    "act":      "Акт",
    "invoice":  "Счёт",
    "contract": "Договор",
    "addendum": "Доп_соглашение",
    "receipt":  "Квитанция",
    "poa":      "Доверенность",
    "cp":       "Коммерческое_предложение",
}

# ====== PDF СТРОИТЕЛИ ======

def header_block(story, title, subtitle=""):
    W = 17 * cm
    story.append(Table([['']], colWidths=[W], rowHeights=[0.5 * cm],
        style=TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1a1a2e'))])))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(title, S(bold=True, size=14, align=TA_CENTER, color='#1a1a2e', space=4)))
    if subtitle:
        story.append(Paragraph(subtitle, S(size=9, align=TA_CENTER, color='#666666', space=4)))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1, 0.4 * cm))

def parties_block(story, executor, client, exec_phone="", exec_email="", client_phone="", client_email=""):
    def cell(title, name, phone="", email=""):
        items = [
            Paragraph(title, S(bold=True, size=7, color='#888888')),
            Paragraph(name,  S(bold=True, size=9, color='#1a1a2e', space=2)),
        ]
        if phone: items.append(Paragraph(f"Тел: {phone}", S(size=8, color='#444444')))
        if email: items.append(Paragraph(f"Email: {email}", S(size=8, color='#444444')))
        return items

    def wrap(items):
        return [[i] for i in items]

    tl = Table(wrap(cell("ИСПОЛНИТЕЛЬ / СТОРОНА 1", executor, exec_phone, exec_email)),
               colWidths=[7.5 * cm],
               style=TableStyle([('PADDING', (0, 0), (-1, -1), 5), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    tr = Table(wrap(cell("ЗАКАЗЧИК / СТОРОНА 2", client, client_phone, client_email)),
               colWidths=[7.5 * cm],
               style=TableStyle([('PADDING', (0, 0), (-1, -1), 5), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))

    story.append(Table([[tl, tr]], colWidths=[8.5 * cm, 8.5 * cm], style=TableStyle([
        ('BOX',        (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEBEFORE', (1, 0), (1, -1),  0.5, colors.HexColor('#cccccc')),
        ('BACKGROUND', (0, 0), (0, -1),  colors.HexColor('#f5f5fc')),
        ('VALIGN',     (0, 0), (-1, -1), 'TOP'),
    ])))
    story.append(Spacer(1, 0.5 * cm))

def total_block(story, amount, label="ИТОГО К ОПЛАТЕ:"):
    story.append(Table(
        [[Paragraph(label, S(bold=True, size=8, color='#888888')),
          Paragraph(f"{amount} руб.", S(bold=True, size=13, color='#1a1a2e'))]],
        colWidths=[6 * cm, 11 * cm],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#eef1fb')),
            ('BOX',        (0, 0), (-1, -1), 1, colors.HexColor('#1a1a2e')),
            ('PADDING',    (0, 0), (-1, -1), 10),
            ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
        ])))
    story.append(Spacer(1, 0.6 * cm))

def sign_block(story, executor, client):
    story.append(Table([
        [Paragraph("Исполнитель:", S(bold=True, size=8, color='#888888')),
         Paragraph("Заказчик:",    S(bold=True, size=8, color='#888888'))],
        [Paragraph("________________________", S(size=9)),
         Paragraph("________________________", S(size=9))],
        [Paragraph(executor, S(size=8, color='#555555')),
         Paragraph(client,   S(size=8, color='#555555'))],
        [Paragraph("М.П.", S(size=8, color='#aaaaaa')),
         Paragraph("М.П.", S(size=8, color='#aaaaaa'))],
    ], colWidths=[8.5 * cm, 8.5 * cm],
    style=TableStyle([('PADDING', (0, 0), (-1, -1), 5), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')])))

def footer_block(story):
    story.append(Spacer(1, 0.4 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#eeeeee')))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        "Документ сформирован автоматически  •  @samozanyat_bot",
        S(size=7, align=TA_CENTER, color='#aaaaaa')
    ))

def build_pdf(story) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    doc.build(story)
    buf.seek(0)
    return buf.read()

def _clean(text: str) -> str:
    """Убирает markdown-символы из AI-текста перед вставкой в PDF"""
    for ch in ('**', '*', '#', '`'):
        text = text.replace(ch, '')
    return text

def pdf_act(f, text):
    story = []
    header_block(story, "АКТ ВЫПОЛНЕННЫХ РАБОТ",
                 f"№ {doc_number()}  |  Дата: {f.get('date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''),
                  f.get('exec_phone',''), f.get('exec_email',''))
    story.append(Paragraph("ОПИСАНИЕ ВЫПОЛНЕННЫХ РАБОТ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1, 0.15 * cm))
    for line in text.strip().split('\n'):
        if line := _clean(line).strip():
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1, 0.4 * cm))
    total_block(story, f.get('amount', '0'))
    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_invoice(f, text):
    story = []
    header_block(story, "СЧЁТ НА ОПЛАТУ",
                 f"№ {doc_number()}  |  Дата: {f.get('date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''),
                  f.get('exec_phone',''), f.get('exec_email',''))
    story.append(Paragraph("ОПИСАНИЕ УСЛУГ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1, 0.15 * cm))
    for line in text.strip().split('\n'):
        if line := _clean(line).strip():
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1, 0.4 * cm))
    total_block(story, f.get('amount', '0'))
    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_contract(f, text):
    story = []
    header_block(story, "ДОГОВОР ОКАЗАНИЯ УСЛУГ",
                 f"№ {doc_number()}  |  г. {f.get('city','')}, {f.get('date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''),
                  f.get('exec_phone',''), f.get('exec_email',''))
    sections = [
        ("1. ПРЕДМЕТ ДОГОВОРА",
         f"Исполнитель обязуется оказать следующие услуги: {f.get('work','')}"),
        ("2. СТОИМОСТЬ И ПОРЯДОК ОПЛАТЫ",
         f"Стоимость услуг составляет {f.get('amount','')} рублей. "
         "Оплата производится в порядке, согласованном Сторонами."),
        ("3. СРОКИ ИСПОЛНЕНИЯ",
         f"Срок оказания услуг: {f.get('deadline','')}"),
        ("4. ПРАВА И ОБЯЗАННОСТИ СТОРОН",
         "Исполнитель обязуется оказать услуги надлежащего качества в установленные сроки. "
         "Заказчик обязуется принять и оплатить оказанные услуги."),
        ("5. ОТВЕТСТВЕННОСТЬ СТОРОН",
         "Стороны несут ответственность за неисполнение или ненадлежащее исполнение "
         "обязательств в соответствии с действующим законодательством РФ."),
        ("6. ПОРЯДОК РАЗРЕШЕНИЯ СПОРОВ",
         "Споры решаются путём переговоров. При недостижении соглашения — в судебном порядке."),
        ("7. ПРОЧИЕ УСЛОВИЯ", text.strip()),
        ("8. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН", ""),
    ]
    for title, content in sections:
        story.append(Paragraph(title, S(bold=True, size=9, color='#1a1a2e', space=3)))
        if content:
            for line in content.split('\n'):
                if line := _clean(line).strip():
                    story.append(Paragraph(line, S(size=9, space=3)))
        story.append(Spacer(1, 0.3 * cm))
    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_addendum(f, text):
    story = []
    header_block(story, "ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ",
                 f"№ {doc_number()} к Договору № {f.get('contract_num','')} от {f.get('contract_date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''))
    story.append(Paragraph("Стороны договорились внести следующие изменения:", S(size=9, space=5)))
    story.append(Spacer(1, 0.2 * cm))
    for line in text.strip().split('\n'):
        if line := _clean(line).strip():
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Настоящее соглашение является неотъемлемой частью Договора и вступает в силу "
        "с момента подписания обеими Сторонами.", S(size=9, space=5)))
    story.append(Spacer(1, 0.5 * cm))
    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_receipt(f, text):
    story = []
    header_block(story, "КВИТАНЦИЯ ОБ ОПЛАТЕ", f"№ {doc_number()}  |  Дата: {f.get('date','')}")
    data = [
        ["Плательщик:",       f.get('client','')],
        ["Получатель:",       f.get('executor','')],
        ["Назначение:",       f.get('work','')],
        ["Сумма:",            f"{f.get('amount','')} рублей"],
        ["Дата оплаты:",      f.get('date','')],
    ]
    fn_b = 'DV-B' if os.path.exists(FONT_PATH) else 'Helvetica-Bold'
    fn   = 'DV'   if os.path.exists(FONT_PATH) else 'Helvetica'
    story.append(Table(data, colWidths=[5 * cm, 12 * cm], style=TableStyle([
        ('FONTNAME',        (0, 0), (0, -1),  fn_b),
        ('FONTNAME',        (1, 0), (1, -1),  fn),
        ('FONTSIZE',        (0, 0), (-1, -1), 9),
        ('PADDING',         (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS',  (0, 0), (-1, -1), [colors.HexColor('#f5f5fc'), colors.white]),
        ('BOX',             (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEBELOW',       (0, 0), (-1, -2), 0.3, colors.HexColor('#dddddd')),
        ('VALIGN',          (0, 0), (-1, -1), 'MIDDLE'),
    ])))
    story.append(Spacer(1, 0.5 * cm))
    total_block(story, f.get('amount', '0'), "СУММА К ПОЛУЧЕНИЮ:")
    story.append(Paragraph("Подпись получателя: ________________________", S(size=9, space=3)))
    story.append(Paragraph(f.get('executor',''), S(size=8, color='#555555')))
    footer_block(story)
    return build_pdf(story)

def pdf_poa(f, text):
    story = []
    header_block(story, "ДОВЕРЕННОСТЬ", f"г. {f.get('city','')}, {f.get('date','')}")
    story.append(Paragraph(
        f"Я, <b>{f.get('grantor','')}</b>, настоящей доверенностью уполномочиваю "
        f"<b>{f.get('attorney','')}</b> представлять мои интересы:",
        S(size=9, space=8)))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("ПОЛНОМОЧИЯ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1, 0.15 * cm))
    for line in text.strip().split('\n'):
        if line := _clean(line).strip():
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(f"Срок действия: {f.get('validity','')}.", S(size=9, space=5)))
    story.append(Paragraph("Доверенность выдана без права передоверия.", S(size=9, space=10)))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Table([
        [Paragraph("Доверитель:", S(bold=True, size=8, color='#888888')),
         Paragraph("Поверенный:", S(bold=True, size=8, color='#888888'))],
        [Paragraph("________________________", S(size=9)),
         Paragraph("________________________", S(size=9))],
        [Paragraph(f.get('grantor',''), S(size=8, color='#555555')),
         Paragraph(f.get('attorney',''), S(size=8, color='#555555'))],
    ], colWidths=[8.5 * cm, 8.5 * cm],
    style=TableStyle([('PADDING', (0, 0), (-1, -1), 5)])))
    footer_block(story)
    return build_pdf(story)

def pdf_cp(f, text):
    story = []
    header_block(story, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ",
                 f"Дата: {f.get('date','')}  |  Действительно до: {f.get('valid_until','')}")
    story.append(Paragraph(f"Кому: {f.get('client','')}", S(bold=True, size=10, color='#1a1a2e', space=5)))
    story.append(Paragraph(f"От: {f.get('executor','')}", S(size=9, color='#555555', space=10)))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("О НАС:", S(bold=True, size=8, color='#888888')))
    story.append(Paragraph(f.get('about',''), S(size=9, space=8)))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph("МЫ ПРЕДЛАГАЕМ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1, 0.15 * cm))
    for line in text.strip().split('\n'):
        if line := _clean(line).strip():
            story.append(Paragraph(f"• {line}", S(size=9, space=4)))
    story.append(Spacer(1, 0.4 * cm))
    total_block(story, f.get('amount',''), "СТОИМОСТЬ УСЛУГ:")
    story.append(Paragraph("КОНТАКТЫ:", S(bold=True, size=8, color='#888888')))
    if f.get('exec_phone'): story.append(Paragraph(f"Тел: {f['exec_phone']}", S(size=9)))
    if f.get('exec_email'): story.append(Paragraph(f"Email: {f['exec_email']}", S(size=9)))
    footer_block(story)
    return build_pdf(story)

PDF_BUILDERS = {
    "act":      pdf_act,
    "invoice":  pdf_invoice,
    "contract": pdf_contract,
    "addendum": pdf_addendum,
    "receipt":  pdf_receipt,
    "poa":      pdf_poa,
    "cp":       pdf_cp,
}

# ====== AI ГЕНЕРАЦИЯ ======

async def ai(prompt: str) -> str:
    """Запрос к OpenRouter (бесплатная модель)"""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openrouter/free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                },
            )
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"AI ошибка: {e}")
        return "Услуги выполнены в полном объёме и в согласованные сроки."

async def gen_text(doc_type: str, f: dict) -> str:
    w, ex, cl, am, dt = f.get('work',''), f.get('executor',''), f.get('client',''), f.get('amount',''), f.get('date','')
    prompts = {
        "act":      f"3-4 предложения для акта выполненных работ. Исполнитель: {ex}. Заказчик: {cl}. Работа: {w}. Сумма: {am} руб. Дата: {dt}. Только текст, без заголовков.",
        "invoice":  f"2-3 предложения для счёта на оплату. Исполнитель: {ex}. Заказчик: {cl}. Услуга: {w}. Сумма: {am} руб. Только текст.",
        "contract": f"Раздел 'Прочие условия' для договора (3-4 предложения). Услуга: {w}. Исполнитель: {ex}. Без заголовков.",
        "addendum": f"3-4 пункта изменений для доп. соглашения. Суть: {w}. Нумерованный список.",
        "receipt":  f"2 предложения для квитанции. Плательщик: {cl}. Получатель: {ex}. Услуга: {w}. Сумма: {am} руб.",
        "poa":      f"4-5 конкретных полномочий для доверенности. Суть: {w}. Нумерованный список.",
        "cp":       f"5-6 пунктов КП для клиента. Исполнитель: {ex}. Услуга: {w}. Каждый пункт — выгода для клиента.",
    }
    return await ai(prompts.get(doc_type, "Напиши текст документа."))

# ====== ОТПРАВКА PDF ======

async def send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_type: str):
    """Генерирует PDF и отправляет пользователю, обновляет аналитику"""
    await update.message.reply_text("⏳ Генерирую документ...")
    uid = update.effective_user.id
    u   = get_user(uid)

    try:
        text      = await gen_text(doc_type, context.user_data)
        pdf_bytes = PDF_BUILDERS[doc_type](context.user_data, text)

        # Счётчики
        if u["bonus_docs"] > 0:
            u["bonus_docs"] -= 1    # сначала тратим бонусные
        else:
            u["count"] += 1

        # Воронка: документ создан
        u["funnel_doc_created"] = True

        name_part = context.user_data.get('executor', 'doc').split()[0]
        filename  = f"{DOC_LABELS[doc_type]}_{name_part}_{datetime.now().strftime('%d%m%Y')}.pdf"

        # Сохраняем в историю
        u["docs"].append({
            "type":     doc_type,
            "label":    DOC_LABELS[doc_type].replace('_', ' '),
            "filename": filename,
            "date":     datetime.now().strftime('%d.%m.%Y %H:%M'),
            "pdf":      pdf_bytes,   # сохраняем байты для повторного скачивания
        })

        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"✅ *{DOC_LABELS[doc_type].replace('_',' ')} готов!*\n\nСкачайте PDF выше 👆",
            parse_mode='Markdown',
        )

        # Реферальное уведомление при создании первого документа
        if u["count"] == 1 and u.get("referred_by"):
            ref_uid = u["referred_by"]
            if ref_uid in user_data:
                user_data[ref_uid]["referrals"]  += 1
                user_data[ref_uid]["bonus_docs"] += 2  # 2 бонусных за реферала
                try:
                    await context.bot.send_message(
                        chat_id=ref_uid,
                        text="🎉 По вашей ссылке зарегистрировался и создал документ новый пользователь!\n"
                             "Вам начислено *+2 бесплатных документа*.",
                        parse_mode='Markdown',
                    )
                except Exception:
                    pass

        # Лимит-предупреждение
        if not u["paid"] and u["bonus_docs"] == 0 and u["count"] >= FREE_LIMIT:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Оплатить подписку", callback_data="buy"),
                InlineKeyboardButton("👥 Пригласить друга",  callback_data="ref"),
            ]])
            await update.message.reply_text(
                "⚠️ *Бесплатный лимит исчерпан.*\n\n"
                "• Оформите подписку — *350 Stars/мес* (безлимит)\n"
                "• Или пригласите друга — получите *2 бесплатных документа*",
                parse_mode='Markdown', reply_markup=kb,
            )
        else:
            left = "∞" if u["paid"] else (u["bonus_docs"] + max(0, FREE_LIMIT - u["count"]))
            await update.message.reply_text(f"Ещё документ? /new  |  Осталось: {left}")

    except Exception as e:
        logger.error(f"PDF ошибка: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка генерации. Попробуйте: /new")

    return ConversationHandler.END

# ====== ФИЧА 1: УМНОЕ ПРИВЕТСТВИЕ ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    u    = get_user(uid)
    touch(uid)

    # Если пришли по реф-ссылке /start ref_XXXXXXXX
    args = context.args or []
    if args and args[0].startswith("ref_") and not u.get("referred_by"):
        ref_code = args[0][4:]
        for other_uid, other in user_data.items():
            if other.get("ref_code") == ref_code and other_uid != uid:
                u["referred_by"] = other_uid
                break

    # Если профессия уже известна — сразу главное меню
    if u.get("profession"):
        await _show_main_menu(update, u)
        return ConversationHandler.END

    # Иначе — спрашиваем профессию
    kb = ReplyKeyboardMarkup(
        [["🎨 Дизайнер", "💻 Разработчик"],
         ["✍️ Копирайтер", "📸 Фотограф/видео"],
         ["📦 Другой"]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"👋 Привет, {name}! Я создаю профессиональные документы для самозанятых за 30 секунд.\n\n"
        "Кем вы работаете? Покажу нужные вам документы 👇",
        reply_markup=kb,
    )
    return ONBOARD_PROF

async def onboard_profession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняем профессию и показываем персональное меню"""
    uid  = update.effective_user.id
    prof = update.message.text
    u    = get_user(uid)

    if prof not in PROF_DOCS:
        prof = "📦 Другой"

    u["profession"] = prof
    touch(uid)
    await _show_main_menu(update, u, welcome=True)
    return ConversationHandler.END

async def _show_main_menu(update: Update, u: dict, welcome: bool = False):
    """Показывает главное меню с документами для профессии пользователя"""
    prof     = u.get("profession", "📦 Другой")
    doc_list = PROF_DOCS.get(prof, PROF_DOCS["📦 Другой"])
    status   = "✅ Безлимит" if u["paid"] else f"🆓 Осталось: {u['bonus_docs'] + max(0, FREE_LIMIT - u['count'])}"

    # Приветствие только при первом входе
    prefix = (
        f"🎉 Отлично! Для *{prof}* подготовил нужные документы:\n\n"
        if welcome else
        f"📂 Ваши документы ({prof}):\n\n"
    )

    text = (
        f"{prefix}"
        + "\n".join(f"• {d}" for d in doc_list)
        + f"\n\n{status}\n\n"
        "/new — создать документ\n"
        "/mydocs — история документов\n"
        "/ref — пригласить друга\n"
        "/buy — подписка\n"
        "/help — помощь"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Создать документ", callback_data="new_doc"),
    ]])
    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=kb if welcome else None)

# ====== ФИЧА 2: ИСТОРИЯ ДОКУМЕНТОВ /mydocs ======

async def mydocs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    docs = get_user(uid).get("docs", [])
    touch(uid)

    if not docs:
        await update.message.reply_text(
            "📂 У вас пока нет созданных документов.\n\n/new — создать первый")
        return

    await update.message.reply_text(f"📂 *Ваши документы ({len(docs)} шт.):*\n\n"
                                     "Нажмите кнопку для повторного скачивания:",
                                     parse_mode='Markdown')

    # Показываем последние 10 документов
    for i, doc in enumerate(reversed(docs[-10:])):
        real_i = len(docs) - 1 - i   # индекс в исходном списке
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Скачать", callback_data=f"dl_{uid}_{real_i}"),
            InlineKeyboardButton("🗑 Удалить",  callback_data=f"rm_{uid}_{real_i}"),
        ]])
        await update.message.reply_text(
            f"*{doc['label']}*\n🕐 {doc['date']}",
            parse_mode='Markdown', reply_markup=kb,
        )

async def doc_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторная отправка документа из истории"""
    q    = update.callback_query
    await q.answer()
    _, uid_str, idx_str = q.data.split("_", 2)
    uid, idx = int(uid_str), int(idx_str)

    # Проверяем что документ принадлежит этому пользователю
    if q.from_user.id != uid:
        await q.answer("Это чужой документ!", show_alert=True)
        return

    docs = user_data.get(uid, {}).get("docs", [])
    if idx >= len(docs):
        await q.answer("Документ не найден.", show_alert=True)
        return

    doc = docs[idx]
    await q.message.reply_document(
        document=io.BytesIO(doc["pdf"]),
        filename=doc["filename"],
        caption=f"📄 {doc['label']} от {doc['date']}",
    )

async def doc_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление документа из истории"""
    q = update.callback_query
    await q.answer()
    _, uid_str, idx_str = q.data.split("_", 2)
    uid, idx = int(uid_str), int(idx_str)

    if q.from_user.id != uid:
        await q.answer("Это чужой документ!", show_alert=True)
        return

    docs = user_data.get(uid, {}).get("docs", [])
    if idx < len(docs):
        docs.pop(idx)
        await q.message.edit_text("🗑 Документ удалён.")
    else:
        await q.answer("Документ не найден.", show_alert=True)

# ====== ФИЧА 3: АНАЛИТИКА ВОРОНКИ /funnel ======

async def funnel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детальная аналитика для админа"""
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    total      = len(user_data)
    started    = sum(1 for u in user_data.values() if u.get("funnel_started"))
    created    = sum(1 for u in user_data.values() if u.get("funnel_doc_created"))
    paid_users = sum(1 for u in user_data.values() if u.get("paid"))
    total_docs = sum(u.get("count", 0) for u in user_data.values())
    refs_given = sum(u.get("referrals", 0) for u in user_data.values())

    # Конверсии
    def pct(a, b): return f"{a/b*100:.1f}%" if b else "—"

    # Профессии
    prof_counts: dict = {}
    for u in user_data.values():
        p = u.get("profession") or "не указана"
        prof_counts[p] = prof_counts.get(p, 0) + 1
    prof_lines = "\n".join(f"  {p}: {c}" for p, c in sorted(prof_counts.items(), key=lambda x: -x[1]))

    await update.message.reply_text(
        f"📊 *Аналитика воронки*\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"🚀 Нажали /new: {started}  ({pct(started, total)} от всех)\n"
        f"📄 Создали документ: {created}  ({pct(created, started)} от /new)\n"
        f"💳 Оплатили: {paid_users}  ({pct(paid_users, created)} от создавших)\n\n"
        f"📑 Всего документов: {total_docs}\n"
        f"👫 Рефералов приведено: {refs_given}\n\n"
        f"*По профессиям:*\n{prof_lines}",
        parse_mode='Markdown',
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Краткая статистика для админа"""
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    total = len(user_data)
    paid  = sum(1 for u in user_data.values() if u.get("paid"))
    docs  = sum(u.get("count", 0) for u in user_data.values())
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {total}\n"
        f"📄 Документов: {docs}\n"
        f"💳 Подписок: {paid}",
        parse_mode='Markdown',
    )

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    if not user_data:
        await update.message.reply_text("Нет пользователей.")
        return
    msg = "📋 *Последние пользователи:*\n\n"
    for uid, u in list(user_data.items())[-10:]:
        msg += (f"• `{uid}` — {u.get('profession','?')} | "
                f"docs: {u.get('count',0)} | "
                f"paid: {'✅' if u.get('paid') else '❌'}\n")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID: /user 123456789")
        return
    try:
        uid  = int(context.args[0])
        data = user_data.get(uid)
        if not data:
            await update.message.reply_text("Пользователь не найден.")
            return
        await update.message.reply_text(
            f"🔍 *Пользователь* `{uid}`\n\n"
            f"Профессия: {data.get('profession','?')}\n"
            f"📄 Документов: {data.get('count',0)}\n"
            f"🎁 Бонусных: {data.get('bonus_docs',0)}\n"
            f"👥 Рефералов: {data.get('referrals',0)}\n"
            f"💳 Подписка: {'✅' if data.get('paid') else '❌'}\n"
            f"🕐 Активность: {data.get('last_activity','?')}\n"
            f"🔗 Реф-код: {data.get('ref_code','')}",
            parse_mode='Markdown',
        )
    except ValueError:
        await update.message.reply_text("Ошибка: ID должен быть числом")

# ====== ФИЧА 4: АВТООПЛАТА TELEGRAM STARS ======

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню оплаты"""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Оплатить Stars", callback_data="pay_stars"),
        InlineKeyboardButton("💬 Написать @milorky", url="https://t.me/milorky"),
    ]])
    text = (
        "💳 *Подписка — 350 Stars/мес*\n\n"
        "✅ Безлимитные документы\n"
        "✅ Все 7 типов документов\n"
        "✅ PDF с профессиональным дизайном\n"
        "✅ История документов\n\n"
        "Или позовите друга и получите *2 бесплатных документа* /ref"
    )
    msg = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.answer()
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=kb)

async def pay_stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инициирует платёж через Telegram Stars"""
    q = update.callback_query
    await q.answer()
    await context.bot.send_invoice(
        chat_id=q.from_user.id,
        title="Подписка на бот документов",
        description="Безлимитная генерация документов на 30 дней",
        payload="subscription_30d",
        currency="XTR",          # Telegram Stars
        prices=[LabeledPrice("Подписка 30 дней", SUBSCRIPTION_STARS)],
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram вызывает это перед списанием — обязательно ответить OK"""
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После успешной оплаты активируем подписку"""
    uid = update.effective_user.id
    u   = get_user(uid)
    u["paid"]         = True
    u["funnel_paid"]  = True
    touch(uid)

    await update.message.reply_text(
        "🎉 *Оплата получена! Подписка активирована.*\n\n"
        "Теперь документы без ограничений. /new",
        parse_mode='Markdown',
    )
    # Уведомляем админа
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💳 Новая оплата!\nПользователь: `{uid}`\nStars: {SUBSCRIPTION_STARS}",
            parse_mode='Markdown',
        )
    except Exception:
        pass

async def activate_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная активация подписки (запасной вариант для админа)"""
    if not is_admin(update.effective_user):
        return
    try:
        tid = int(context.args[0])
        get_user(tid)["paid"] = True
        await update.message.reply_text(f"✅ Подписка активирована для {tid}")
        await context.bot.send_message(
            chat_id=tid,
            text="🎉 *Подписка активирована!* Документов без ограничений. /new",
            parse_mode='Markdown',
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# ====== ФИЧА 5: РЕФЕРАЛЬНАЯ СИСТЕМА ======

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает реферальную ссылку пользователя"""
    uid  = update.effective_user.id
    u    = get_user(uid)
    code = u.get("ref_code", _make_ref(uid))
    touch(uid)

    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{code}"

    await update.message.reply_text(
        f"👥 *Реферальная программа*\n\n"
        f"За каждого друга, который создаст первый документ:\n"
        f"• Вы получаете *+2 бесплатных документа*\n"
        f"• Другу тоже приятно 😊\n\n"
        f"🔗 Ваша ссылка:\n`{ref_link}`\n\n"
        f"📊 Вы пригласили: *{u.get('referrals', 0)}* чел.\n"
        f"🎁 Бонусных документов: *{u.get('bonus_docs', 0)}*",
        parse_mode='Markdown',
    )

# ====== ОСНОВНЫЕ ХЕНДЛЕРЫ ======

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Как пользоваться:*\n\n"
        "1. /new — создать документ\n"
        "2. /mydocs — история документов\n"
        "3. /ref — пригласить друга (+2 doc)\n"
        "4. /buy — подписка (безлимит)\n\n"
        "❓ Если поле не нужно — напишите «-»\n\n"
        "По вопросам: @milorky",
        parse_mode='Markdown',
    )

async def new_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u   = get_user(uid)
    touch(uid)
    u["funnel_started"] = True  # воронка: нажал /new

    if not can_generate(uid):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Оформить подписку", callback_data="buy"),
            InlineKeyboardButton("👥 Пригласить друга",  callback_data="ref"),
        ]])
        await update.message.reply_text(
            "⚠️ *Лимит исчерпан*\n\n"
            "• Подписка — *350 Stars/мес*, безлимит\n"
            "• Или позовите друга — получите *2 бесплатных документа*",
            parse_mode='Markdown', reply_markup=kb,
        )
        return ConversationHandler.END

    # Персональный список документов по профессии
    prof     = u.get("profession", "📦 Другой")
    doc_list = PROF_DOCS.get(prof, PROF_DOCS["📦 Другой"])

    # Разбиваем на строки по 2
    rows = [doc_list[i:i+2] for i in range(0, len(doc_list), 2)]

    await update.message.reply_text(
        "📂 *Выберите документ:*",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True),
    )
    return CHOOSE_DOC

async def choose_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = update.message.text
    rm   = ReplyKeyboardRemove()

    mapping = {
        "Акт":          ("act",      "📄 Акт выполненных работ", "ACT_1",  ACT_1),
        "Счёт":         ("invoice",  "💰 Счёт на оплату",        "INV_1",  INV_1),
        "Договор":      ("contract", "📃 Договор оказания услуг","CONTRACT_1", CONTRACT_1),
        "Доп":          ("addendum", "📝 Доп. соглашение",       "ADDENDUM_1", ADDENDUM_1),
        "Квитанция":    ("receipt",  "🧾 Квитанция об оплате",   "RECEIPT_1", RECEIPT_1),
        "Доверенность": ("poa",      "📋 Доверенность",          "POA_1",  POA_1),
        "Коммерческое": ("cp",       "📊 Коммерческое предложение", "CP_1", CP_1),
    }

    steps = {
        "act":      6, "invoice": 5, "contract": 6,
        "addendum": 4, "receipt": 4, "poa": 4, "cp": 5,
    }
    first_q = {
        "act":      "Ваше ФИО\n\nПример: Иванов Иван Иванович",
        "invoice":  "Ваше ФИО\n\nПример: Иванов Иван Иванович",
        "contract": "Ваше ФИО (исполнитель)\n\nПример: Иванов Иван Иванович",
        "addendum": "Ваше ФИО (исполнитель)",
        "receipt":  "Ваше ФИО (получатель)",
        "poa":      "Ваше ФИО (доверитель)\n\nПример: Иванов Иван Иванович",
        "cp":       "Ваше ФИО / название компании",
    }

    for keyword, (doc_type, label, _, state) in mapping.items():
        if keyword in text:
            context.user_data["doc_type"] = doc_type
            n = steps[doc_type]
            q = first_q[doc_type]
            await update.message.reply_text(
                f"{label}\n\n*Шаг 1/{n}* — {q}",
                parse_mode='Markdown', reply_markup=rm,
            )
            return state

    await update.message.reply_text("Выберите из меню.")
    return CHOOSE_DOC

# ---- АКТ ----
async def act_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/6* — Ваш телефон _(или «-»)_", parse_mode='Markdown'); return ACT_2
async def act_2(u, c): c.user_data["exec_phone"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 3/6* — Ваш email _(или «-»)_", parse_mode='Markdown'); return ACT_3
async def act_3(u, c): c.user_data["exec_email"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 4/6* — Название заказчика", parse_mode='Markdown'); return ACT_4
async def act_4(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 5/6* — Что сделали?\n\nПример: Разработка сайта", parse_mode='Markdown'); return ACT_5
async def act_5(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 6/6* — Сумма и дата\n\nПример: 15000, 26 июня 2026", parse_mode='Markdown'); return ACT_6
async def act_6(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = p[0]
    c.user_data["date"]   = p[1] if len(p) > 1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "act")

# ---- СЧЁТ ----
async def inv_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/5* — Телефон _(или «-»)_", parse_mode='Markdown'); return INV_2
async def inv_2(u, c): c.user_data["exec_phone"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 3/5* — Email _(или «-»)_", parse_mode='Markdown'); return INV_3
async def inv_3(u, c): c.user_data["exec_email"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 4/5* — Название заказчика", parse_mode='Markdown'); return INV_4
async def inv_4(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 5/5* — Услуга, сумма, дата\n\nПример: Дизайн сайта, 25000, 26 июня 2026", parse_mode='Markdown'); return INV_5
async def inv_5(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["work"]   = p[0]
    c.user_data["amount"] = p[1] if len(p) > 1 else ""
    c.user_data["date"]   = p[2] if len(p) > 2 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "invoice")

# ---- ДОГОВОР ----
async def contract_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/6* — Телефон и email\n\nПример: +7 999 123-45-67, ivan@mail.ru _(или «-»)_", parse_mode='Markdown'); return CONTRACT_2
async def contract_2(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["exec_phone"] = "" if u.message.text=="-" else p[0]
    c.user_data["exec_email"] = p[1] if len(p) > 1 else ""
    await u.message.reply_text("*Шаг 3/6* — Название заказчика", parse_mode='Markdown'); return CONTRACT_3
async def contract_3(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 4/6* — Что делаете? (предмет договора)", parse_mode='Markdown'); return CONTRACT_4
async def contract_4(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 5/6* — Стоимость и срок\n\nПример: 30000, 30 дней", parse_mode='Markdown'); return CONTRACT_5
async def contract_5(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"]   = p[0]
    c.user_data["deadline"] = p[1] if len(p) > 1 else "по договорённости"
    await u.message.reply_text("*Шаг 6/6* — Город и дата\n\nПример: Москва, 26 июня 2026", parse_mode='Markdown'); return CONTRACT_6
async def contract_6(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["city"] = p[0]
    c.user_data["date"] = p[1] if len(p) > 1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "contract")

# ---- ДОП. СОГЛАШЕНИЕ ----
async def addendum_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — Название заказчика", parse_mode='Markdown'); return ADDENDUM_2
async def addendum_2(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — Номер и дата договора\n\nПример: 12, 15 января 2026", parse_mode='Markdown'); return ADDENDUM_3
async def addendum_3(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["contract_num"]  = p[0]
    c.user_data["contract_date"] = p[1] if len(p) > 1 else ""
    await u.message.reply_text("*Шаг 4/4* — Суть изменений", parse_mode='Markdown'); return ADDENDUM_4
async def addendum_4(u, c): c.user_data["work"] = u.message.text; return await send_pdf(u, c, "addendum")

# ---- КВИТАНЦИЯ ----
async def receipt_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — Плательщик", parse_mode='Markdown'); return RECEIPT_2
async def receipt_2(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — За что оплата?", parse_mode='Markdown'); return RECEIPT_3
async def receipt_3(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 4/4* — Сумма и дата\n\nПример: 10000, 26 июня 2026", parse_mode='Markdown'); return RECEIPT_4
async def receipt_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = p[0]
    c.user_data["date"]   = p[1] if len(p) > 1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "receipt")

# ---- ДОВЕРЕННОСТЬ ----
async def poa_1(u, c): c.user_data["grantor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — ФИО поверенного", parse_mode='Markdown'); return POA_2
async def poa_2(u, c): c.user_data["attorney"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — Суть полномочий\n\nПример: подписание договоров, получение оплаты", parse_mode='Markdown'); return POA_3
async def poa_3(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 4/4* — Срок и город\n\nПример: 1 год, Москва", parse_mode='Markdown'); return POA_4
async def poa_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["validity"] = p[0]
    c.user_data["city"]     = p[1] if len(p) > 1 else ""
    c.user_data["date"]     = datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "poa")

# ---- КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ ----
async def cp_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/5* — Телефон и email\n\nПример: +7 999 123-45-67, ivan@mail.ru _(или «-»)_", parse_mode='Markdown'); return CP_2
async def cp_2(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["exec_phone"] = "" if u.message.text=="-" else p[0]
    c.user_data["exec_email"] = p[1] if len(p) > 1 else ""
    await u.message.reply_text("*Шаг 3/5* — Кому КП?\n\nПример: ООО «Ромашка»", parse_mode='Markdown'); return CP_3
async def cp_3(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 4/5* — Что предлагаете и стоимость\n\nПример: Разработка сайта, 80000", parse_mode='Markdown'); return CP_4
async def cp_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["work"]   = p[0]
    c.user_data["amount"] = p[1] if len(p) > 1 else ""
    await u.message.reply_text("*Шаг 5/5* — Кратко о себе\n\nПример: 5 лет опыта, 200+ проектов", parse_mode='Markdown'); return CP_5
async def cp_5(u, c):
    c.user_data["about"]       = u.message.text
    c.user_data["date"]        = datetime.now().strftime('%d.%m.%Y')
    c.user_data["valid_until"] = "30 дней"
    return await send_pdf(u, c, "cp")

# ====== РАЗНОЕ ======

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш ID: `{update.effective_user.id}`\n\nОтправьте @milorky после оплаты.",
        parse_mode='Markdown',
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /new — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Центральный роутер инлайн-кнопок"""
    q    = update.callback_query
    data = q.data

    if data == "buy":
        await buy(update, context)
    elif data == "pay_stars":
        await pay_stars_callback(update, context)
    elif data == "ref":
        await q.answer()
        await ref(update, context)
    elif data == "new_doc":
        await q.answer()
        await new_doc(update, context)
    elif data.startswith("dl_"):
        await doc_download_callback(update, context)
    elif data.startswith("rm_"):
        await doc_remove_callback(update, context)
    else:
        await q.answer()

# ====== MAIN ======

def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    download_fonts()

    app = Application.builder().token(BOT_TOKEN).build()

    # Онбординг ConversationHandler — отдельный, чтобы не конфликтовал с /new
    onboard_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ONBOARD_PROF: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_profession)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Основной ConversationHandler для создания документов
    doc_conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_doc)],
        states={
            CHOOSE_DOC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_doc)],
            ACT_1:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_1)],
            ACT_2:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_2)],
            ACT_3:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_3)],
            ACT_4:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_4)],
            ACT_5:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_5)],
            ACT_6:        [MessageHandler(filters.TEXT & ~filters.COMMAND, act_6)],
            INV_1:        [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_1)],
            INV_2:        [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_2)],
            INV_3:        [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_3)],
            INV_4:        [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_4)],
            INV_5:        [MessageHandler(filters.TEXT & ~filters.COMMAND, inv_5)],
            CONTRACT_1:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_1)],
            CONTRACT_2:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_2)],
            CONTRACT_3:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_3)],
            CONTRACT_4:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_4)],
            CONTRACT_5:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_5)],
            CONTRACT_6:   [MessageHandler(filters.TEXT & ~filters.COMMAND, contract_6)],
            ADDENDUM_1:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_1)],
            ADDENDUM_2:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_2)],
            ADDENDUM_3:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_3)],
            ADDENDUM_4:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_4)],
            RECEIPT_1:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_1)],
            RECEIPT_2:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_2)],
            RECEIPT_3:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_3)],
            RECEIPT_4:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_4)],
            POA_1:        [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_1)],
            POA_2:        [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_2)],
            POA_3:        [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_3)],
            POA_4:        [MessageHandler(filters.TEXT & ~filters.COMMAND, poa_4)],
            CP_1:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_1)],
            CP_2:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_2)],
            CP_3:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_3)],
            CP_4:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_4)],
            CP_5:         [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_5)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Регистрируем хендлеры
    app.add_handler(onboard_conv)
    app.add_handler(doc_conv)
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
    # Платёжные хендлеры
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    # Инлайн-кнопки
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Бот запущен ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
