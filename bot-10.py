import logging
import os
import io
import httpx
import urllib.request
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
ADMIN_USERNAME = "milorky"
FREE_LIMIT = 2
user_data = {}

# Состояния
(CHOOSE_DOC,
 ACT_1, ACT_2, ACT_3, ACT_4, ACT_5, ACT_6,
 INV_1, INV_2, INV_3, INV_4, INV_5,
 CONTRACT_1, CONTRACT_2, CONTRACT_3, CONTRACT_4, CONTRACT_5, CONTRACT_6,
 ADDENDUM_1, ADDENDUM_2, ADDENDUM_3, ADDENDUM_4,
 RECEIPT_1, RECEIPT_2, RECEIPT_3, RECEIPT_4,
 POA_1, POA_2, POA_3, POA_4,
 CP_1, CP_2, CP_3, CP_4, CP_5) = range(35)

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DejaVuSans-Bold.ttf")

def download_fonts():
    try:
        if os.path.exists(FONT_PATH) and os.path.exists(FONT_BOLD_PATH):
            pdfmetrics.registerFont(TTFont('DV', FONT_PATH))
            pdfmetrics.registerFont(TTFont('DV-B', FONT_BOLD_PATH))
            logger.info("Fonts loaded OK")
            return True
        else:
            logger.warning("Font files not found in bot folder")
            return False
    except Exception as e:
        logger.warning(f"Font load failed: {e}")
        return False

def S(bold=False, size=9, align=TA_LEFT, color='#222222', space=3):
    fn = 'DV-B' if bold else 'DV'
    if not os.path.exists(FONT_PATH):
        fn = 'Helvetica-Bold' if bold else 'Helvetica'
    return ParagraphStyle('x', fontName=fn, fontSize=size,
                          alignment=align, spaceAfter=space,
                          leading=size*1.4, textColor=colors.HexColor(color))

def get_user(uid):
    if uid not in user_data:
        user_data[uid] = {"count": 0, "paid": False}
    return user_data[uid]

def can_generate(uid):
    u = get_user(uid)
    return u["paid"] or u["count"] < FREE_LIMIT

def doc_number():
    return datetime.now().strftime('%Y%m%d-%H%M')

def header_block(story, title, subtitle=""):
    W = 17*cm
    story.append(Table([['']], colWidths=[W], rowHeights=[0.5*cm],
        style=TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#1a1a2e'))])))
    story.append(Spacer(1,0.4*cm))
    story.append(Paragraph(title, S(bold=True, size=14, align=TA_CENTER, color='#1a1a2e', space=4)))
    if subtitle:
        story.append(Paragraph(subtitle, S(size=9, align=TA_CENTER, color='#666666', space=4)))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1,0.4*cm))

def parties_block(story, executor, client, exec_phone="", exec_email="", client_phone="", client_email=""):
    def cell(title, name, phone="", email=""):
        items = [Paragraph(title, S(bold=True, size=7, color='#888888')),
                 Paragraph(name, S(bold=True, size=9, color='#1a1a2e', space=2))]
        if phone: items.append(Paragraph(f"Тел: {phone}", S(size=8, color='#444444')))
        if email: items.append(Paragraph(f"Email: {email}", S(size=8, color='#444444')))
        return items

    left = cell("ИСПОЛНИТЕЛЬ / СТОРОНА 1", executor, exec_phone, exec_email)
    right = cell("ЗАКАЗЧИК / СТОРОНА 2", client, client_phone, client_email)

    def wrap(items):
        return [[item] for item in items]

    tl = Table(wrap(left), colWidths=[7.5*cm],
               style=TableStyle([('PADDING',(0,0),(-1,-1),5),('VALIGN',(0,0),(-1,-1),'TOP')]))
    tr = Table(wrap(right), colWidths=[7.5*cm],
               style=TableStyle([('PADDING',(0,0),(-1,-1),5),('VALIGN',(0,0),(-1,-1),'TOP')]))

    t = Table([[tl, tr]], colWidths=[8.5*cm,8.5*cm],
        style=TableStyle([
            ('BOX',(0,0),(-1,-1),0.5,colors.HexColor('#cccccc')),
            ('LINEBEFORE',(1,0),(1,-1),0.5,colors.HexColor('#cccccc')),
            ('BACKGROUND',(0,0),(0,-1),colors.HexColor('#f5f5fc')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
    story.append(t)
    story.append(Spacer(1,0.5*cm))

def total_block(story, amount, label="ИТОГО К ОПЛАТЕ:"):
    story.append(Table(
        [[Paragraph(label, S(bold=True, size=8, color='#888888')),
          Paragraph(f"{amount} руб.", S(bold=True, size=13, color='#1a1a2e'))]],
        colWidths=[6*cm,11*cm],
        style=TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#eef1fb')),
            ('BOX',(0,0),(-1,-1),1,colors.HexColor('#1a1a2e')),
            ('PADDING',(0,0),(-1,-1),10),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ])))
    story.append(Spacer(1,0.6*cm))

def sign_block(story, executor, client):
    t = Table([
        [Paragraph("Исполнитель:", S(bold=True, size=8, color='#888888')),
         Paragraph("Заказчик:", S(bold=True, size=8, color='#888888'))],
        [Paragraph("________________________", S(size=9)),
         Paragraph("________________________", S(size=9))],
        [Paragraph(executor, S(size=8, color='#555555')),
         Paragraph(client, S(size=8, color='#555555'))],
        [Paragraph("М.П.", S(size=8, color='#aaaaaa')),
         Paragraph("М.П.", S(size=8, color='#aaaaaa'))],
    ], colWidths=[8.5*cm,8.5*cm],
    style=TableStyle([('PADDING',(0,0),(-1,-1),5),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    story.append(t)

def footer_block(story):
    story.append(Spacer(1,0.4*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#eeeeee')))
    story.append(Spacer(1,0.2*cm))
    story.append(Paragraph("Документ сформирован автоматически  •  @samozanybot",
                           S(size=7, align=TA_CENTER, color='#aaaaaa')))

def build_pdf(story) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    doc.build(story)
    buf.seek(0)
    return buf.read()

# ====== ГЕНЕРАТОРЫ PDF ======

def pdf_act(f, text):
    story = []
    header_block(story, "АКТ ВЫПОЛНЕННЫХ РАБОТ",
                 f"№ {doc_number()}  |  Дата: {f.get('date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''),
                  f.get('exec_phone',''), f.get('exec_email',''))
    story.append(Paragraph("ОПИСАНИЕ ВЫПОЛНЕННЫХ РАБОТ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1,0.15*cm))
    for line in text.strip().split('\n'):
        line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
        if line:
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1,0.4*cm))
    total_block(story, f.get('amount','0'))
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
    story.append(Spacer(1,0.15*cm))
    for line in text.strip().split('\n'):
        line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
        if line:
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1,0.4*cm))
    total_block(story, f.get('amount','0'))
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
        ("1. ПРЕДМЕТ ДОГОВОРА", f"Исполнитель обязуется оказать следующие услуги: {f.get('work','')}"),
        ("2. СТОИМОСТЬ И ПОРЯДОК ОПЛАТЫ", f"Стоимость услуг составляет {f.get('amount','')} рублей. "
         f"Оплата производится в порядке, согласованном Сторонами."),
        ("3. СРОКИ ИСПОЛНЕНИЯ", f"Срок оказания услуг: {f.get('deadline','')}"),
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
                line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
                if line:
                    story.append(Paragraph(line, S(size=9, space=3)))
        story.append(Spacer(1,0.3*cm))

    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_addendum(f, text):
    story = []
    header_block(story, "ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ",
                 f"№ {doc_number()} к Договору № {f.get('contract_num','')} от {f.get('contract_date','')}")
    parties_block(story, f.get('executor',''), f.get('client',''))
    story.append(Paragraph("Стороны договорились внести следующие изменения в договор:",
                           S(size=9, space=5)))
    story.append(Spacer(1,0.2*cm))
    for line in text.strip().split('\n'):
        line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
        if line:
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1,0.4*cm))
    story.append(Paragraph(
        "Настоящее соглашение является неотъемлемой частью Договора и вступает в силу "
        "с момента подписания обеими Сторонами.", S(size=9, space=5)))
    story.append(Spacer(1,0.5*cm))
    sign_block(story, f.get('executor',''), f.get('client',''))
    footer_block(story)
    return build_pdf(story)

def pdf_receipt(f, text):
    story = []
    header_block(story, "КВИТАНЦИЯ ОБ ОПЛАТЕ", f"№ {doc_number()}  |  Дата: {f.get('date','')}")

    data = [
        ["Плательщик:", f.get('client','')],
        ["Получатель:", f.get('executor','')],
        ["Назначение платежа:", f.get('work','')],
        ["Сумма:", f"{f.get('amount','')} рублей"],
        ["Дата оплаты:", f.get('date','')],
    ]
    t = Table(data, colWidths=[5*cm, 12*cm],
        style=TableStyle([
            ('FONTNAME',(0,0),(0,-1),'DV-B' if os.path.exists(FONT_PATH) else 'Helvetica-Bold'),
            ('FONTNAME',(1,0),(1,-1),'DV' if os.path.exists(FONT_PATH) else 'Helvetica'),
            ('FONTSIZE',(0,0),(-1,-1),9),
            ('PADDING',(0,0),(-1,-1),8),
            ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor('#f5f5fc'), colors.white]),
            ('BOX',(0,0),(-1,-1),0.5,colors.HexColor('#cccccc')),
            ('LINEBELOW',(0,0),(-1,-2),0.3,colors.HexColor('#dddddd')),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ]))
    story.append(t)
    story.append(Spacer(1,0.5*cm))
    total_block(story, f.get('amount','0'), "СУММА К ПОЛУЧЕНИЮ:")
    story.append(Spacer(1,0.3*cm))
    story.append(Paragraph("Подпись получателя: ________________________",
                           S(size=9, space=3)))
    story.append(Paragraph(f.get('executor',''), S(size=8, color='#555555')))
    footer_block(story)
    return build_pdf(story)

def pdf_poa(f, text):
    story = []
    header_block(story, "ДОВЕРЕННОСТЬ",
                 f"г. {f.get('city','')}, {f.get('date','')}")
    story.append(Paragraph(
        f"Я, <b>{f.get('grantor','')}</b>, настоящей доверенностью уполномочиваю "
        f"<b>{f.get('attorney','')}</b> представлять мои интересы в следующих вопросах:",
        S(size=9, space=8)))
    story.append(Spacer(1,0.3*cm))
    story.append(Paragraph("ПОЛНОМОЧИЯ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1,0.15*cm))
    for line in text.strip().split('\n'):
        line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
        if line:
            story.append(Paragraph(line, S(size=9, space=3)))
    story.append(Spacer(1,0.4*cm))
    story.append(Paragraph(
        f"Срок действия доверенности: {f.get('validity','')}.",
        S(size=9, space=5)))
    story.append(Paragraph(
        "Доверенность выдана без права передоверия.",
        S(size=9, space=10)))
    story.append(Spacer(1,0.5*cm))
    t = Table([
        [Paragraph("Доверитель:", S(bold=True, size=8, color='#888888')),
         Paragraph("Поверенный:", S(bold=True, size=8, color='#888888'))],
        [Paragraph("________________________", S(size=9)),
         Paragraph("________________________", S(size=9))],
        [Paragraph(f.get('grantor',''), S(size=8, color='#555555')),
         Paragraph(f.get('attorney',''), S(size=8, color='#555555'))],
    ], colWidths=[8.5*cm,8.5*cm],
    style=TableStyle([('PADDING',(0,0),(-1,-1),5)]))
    story.append(t)
    footer_block(story)
    return build_pdf(story)

def pdf_cp(f, text):
    story = []
    header_block(story, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ",
                 f"Дата: {f.get('date','')}  |  Действительно до: {f.get('valid_until','')}")

    story.append(Paragraph(f"Кому: {f.get('client','')}", S(bold=True, size=10, color='#1a1a2e', space=5)))
    story.append(Paragraph(f"От: {f.get('executor','')}", S(size=9, color='#555555', space=10)))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1,0.4*cm))

    story.append(Paragraph("О НАС:", S(bold=True, size=8, color='#888888')))
    story.append(Paragraph(f.get('about',''), S(size=9, space=8)))
    story.append(Spacer(1,0.2*cm))

    story.append(Paragraph("МЫ ПРЕДЛАГАЕМ:", S(bold=True, size=8, color='#888888')))
    story.append(Spacer(1,0.15*cm))
    for line in text.strip().split('\n'):
        line = line.strip().replace('**','').replace('*','').replace('#','').replace('`','')
        if line:
            story.append(Paragraph(f"• {line}", S(size=9, space=4)))
    story.append(Spacer(1,0.4*cm))

    total_block(story, f.get('amount',''), "СТОИМОСТЬ УСЛУГ:")

    story.append(Paragraph("КОНТАКТЫ ДЛЯ СВЯЗИ:", S(bold=True, size=8, color='#888888')))
    if f.get('exec_phone'):
        story.append(Paragraph(f"Тел: {f.get('exec_phone','')}", S(size=9)))
    if f.get('exec_email'):
        story.append(Paragraph(f"Email: {f.get('exec_email','')}", S(size=9)))
    footer_block(story)
    return build_pdf(story)


# ====== AI ГЕНЕРАЦИЯ ======

async def ai(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model":"openrouter/free","messages":[{"role":"user","content":prompt}],"max_tokens":500}
        )
        return r.json()["choices"][0]["message"]["content"]

async def gen_text(doc_type, f):
    w = f.get('work','')
    ex = f.get('executor','')
    cl = f.get('client','')
    am = f.get('amount','')
    dt = f.get('date','')

    prompts = {
        "act": f"3-4 предложения для акта выполненных работ. Исполнитель: {ex}. Заказчик: {cl}. Работа: {w}. Сумма: {am} руб. Дата: {dt}. Только текст, без заголовков.",
        "invoice": f"2-3 предложения для счёта на оплату. Исполнитель: {ex}. Заказчик: {cl}. Услуга: {w}. Сумма: {am} руб. Дата: {dt}. Только текст, без заголовков.",
        "contract": f"Напиши раздел 'Прочие условия' для договора оказания услуг (3-4 предложения). Услуга: {w}. Исполнитель: {ex}. Заказчик: {cl}. Без заголовков.",
        "addendum": f"Напиши 3-4 чётких пункта изменений для дополнительного соглашения к договору. Суть изменений: {w}. Оформи как нумерованный список.",
        "receipt": f"2 предложения для квитанции об оплате. Плательщик: {cl}. Получатель: {ex}. Услуга: {w}. Сумма: {am} руб. Только текст.",
        "poa": f"Напиши 4-5 конкретных полномочий для доверенности. Суть: {w}. Оформи нумерованным списком.",
        "cp": f"Напиши 5-6 пунктов коммерческого предложения для клиента. Исполнитель: {ex}. Услуга: {w}. Каждый пункт — одно предложение о выгоде для клиента.",
    }
    return await ai(prompts.get(doc_type, "Напиши текст документа."))


# ====== ОТПРАВКА PDF ======

PDF_BUILDERS = {
    "act": pdf_act,
    "invoice": pdf_invoice,
    "contract": pdf_contract,
    "addendum": pdf_addendum,
    "receipt": pdf_receipt,
    "poa": pdf_poa,
    "cp": pdf_cp,
}

DOC_LABELS = {
    "act": "Акт",
    "invoice": "Счёт",
    "contract": "Договор",
    "addendum": "Доп_соглашение",
    "receipt": "Квитанция",
    "poa": "Доверенность",
    "cp": "Коммерческое_предложение",
}

async def send_pdf(update, context, doc_type):
    await update.message.reply_text("⏳ Генерирую документ, создаю PDF...")
    try:
        text = await gen_text(doc_type, context.user_data)
        builder = PDF_BUILDERS[doc_type]
        pdf_bytes = builder(context.user_data, text)

        uid = update.effective_user.id
        user_data[uid]["count"] += 1
        u = get_user(uid)

        name_part = context.user_data.get('executor', 'doc').split()[0]
        filename = f"{DOC_LABELS[doc_type]}_{name_part}_{datetime.now().strftime('%d%m%Y')}.pdf"

        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"✅ *{DOC_LABELS[doc_type].replace('_',' ')} готов!*\n\nСкачайте PDF выше 👆",
            parse_mode='Markdown'
        )

        if not u["paid"] and u["count"] >= FREE_LIMIT:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Подписка — 299 ₽/мес", callback_data="buy")]])
            await update.message.reply_text("⚠️ Последний бесплатный документ использован.", reply_markup=kb)
        else:
            left = "∞" if u["paid"] else FREE_LIMIT - u["count"]
            await update.message.reply_text(f"Ещё документ? /new  |  Осталось бесплатных: {left}")

    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text("❌ Ошибка. Попробуйте: /new")
    return ConversationHandler.END


# ====== HANDLERS ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    name = update.effective_user.first_name
    status = "✅ Подписка активна — без ограничений" if u["paid"] else f"🆓 Бесплатных: {FREE_LIMIT - u['count']} из {FREE_LIMIT}"
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Создаю профессиональные документы для самозанятых за 30 секунд.\n\n"
        "📄 Акт выполненных работ\n"
        "💰 Счёт на оплату\n"
        "📃 Договор оказания услуг\n"
        "📝 Доп. соглашение\n"
        "🧾 Квитанция об оплате\n"
        "📋 Доверенность\n"
        "📊 Коммерческое предложение\n\n"
        f"{status}\n\n"
        "/new — создать документ\n"
        "/buy — подписка 299 ₽/мес\n"
        "/help — помощь",
        reply_markup=ReplyKeyboardRemove()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Как пользоваться:*\n\n"
        "1. /new — начать создание документа\n"
        "2. Выбери тип документа\n"
        "3. Ответь на вопросы бота\n"
        "4. Получи готовый PDF!\n\n"
        "❓ Если не нужно поле — напиши «-»\n\n"
        "По вопросам: @milorky",
        parse_mode='Markdown'
    )

async def new_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_generate(update.effective_user.id):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оформить подписку", callback_data="buy")]])
        await update.message.reply_text(
            "⚠️ *Лимит исчерпан*\n\nПодписка — *299 ₽/мес*, неограниченные документы.",
            parse_mode='Markdown', reply_markup=kb)
        return ConversationHandler.END

    kb = [
        ["📄 Акт выполненных работ", "💰 Счёт на оплату"],
        ["📃 Договор оказания услуг", "📝 Доп. соглашение"],
        ["🧾 Квитанция об оплате", "📋 Доверенность"],
        ["📊 Коммерческое предложение"],
    ]
    await update.message.reply_text(
        "📂 *Выберите документ:*",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
    )
    return CHOOSE_DOC

async def choose_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = update.message.text
    rm = ReplyKeyboardRemove()

    if "Акт" in text:
        context.user_data["doc_type"] = "act"
        await update.message.reply_text("📄 *Акт выполненных работ*\n\n*Шаг 1/6* — Ваше ФИО\n\nПример: Иванов Иван Иванович",
            parse_mode='Markdown', reply_markup=rm)
        return ACT_1
    elif "Счёт" in text:
        context.user_data["doc_type"] = "invoice"
        await update.message.reply_text("💰 *Счёт на оплату*\n\n*Шаг 1/5* — Ваше ФИО\n\nПример: Иванов Иван Иванович",
            parse_mode='Markdown', reply_markup=rm)
        return INV_1
    elif "Договор" in text:
        context.user_data["doc_type"] = "contract"
        await update.message.reply_text("📃 *Договор оказания услуг*\n\n*Шаг 1/6* — Ваше ФИО (исполнитель)\n\nПример: Иванов Иван Иванович",
            parse_mode='Markdown', reply_markup=rm)
        return CONTRACT_1
    elif "Доп" in text:
        context.user_data["doc_type"] = "addendum"
        await update.message.reply_text("📝 *Доп. соглашение*\n\n*Шаг 1/4* — Ваше ФИО (исполнитель)",
            parse_mode='Markdown', reply_markup=rm)
        return ADDENDUM_1
    elif "Квитанция" in text:
        context.user_data["doc_type"] = "receipt"
        await update.message.reply_text("🧾 *Квитанция об оплате*\n\n*Шаг 1/4* — Ваше ФИО (получатель)",
            parse_mode='Markdown', reply_markup=rm)
        return RECEIPT_1
    elif "Доверенность" in text:
        context.user_data["doc_type"] = "poa"
        await update.message.reply_text("📋 *Доверенность*\n\n*Шаг 1/4* — Ваше ФИО (доверитель)\n\nПример: Иванов Иван Иванович",
            parse_mode='Markdown', reply_markup=rm)
        return POA_1
    elif "Коммерческое" in text:
        context.user_data["doc_type"] = "cp"
        await update.message.reply_text("📊 *Коммерческое предложение*\n\n*Шаг 1/5* — Ваше ФИО / название компании",
            parse_mode='Markdown', reply_markup=rm)
        return CP_1
    else:
        await update.message.reply_text("Выберите из меню.")
        return CHOOSE_DOC

# ---- АКТ ----
async def act_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/6* — Ваш телефон\n_(или «-»)_", parse_mode='Markdown'); return ACT_2
async def act_2(u, c): c.user_data["exec_phone"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 3/6* — Ваш email\n_(или «-»)_", parse_mode='Markdown'); return ACT_3
async def act_3(u, c): c.user_data["exec_email"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 4/6* — Название заказчика", parse_mode='Markdown'); return ACT_4
async def act_4(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 5/6* — Что вы сделали?\n\nПример: Разработка сайта", parse_mode='Markdown'); return ACT_5
async def act_5(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 6/6* — Сумма и дата\n\nПример: 15000, 26 июня 2026", parse_mode='Markdown'); return ACT_6
async def act_6(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = p[0]; c.user_data["date"] = p[1] if len(p)>1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "act")

# ---- СЧЁТ ----
async def inv_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/5* — Ваш телефон\n_(или «-»)_", parse_mode='Markdown'); return INV_2
async def inv_2(u, c): c.user_data["exec_phone"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 3/5* — Ваш email\n_(или «-»)_", parse_mode='Markdown'); return INV_3
async def inv_3(u, c): c.user_data["exec_email"] = "" if u.message.text=="-" else u.message.text; await u.message.reply_text("*Шаг 4/5* — Название заказчика", parse_mode='Markdown'); return INV_4
async def inv_4(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 5/5* — Услуга, сумма и дата\n\nПример: Дизайн сайта, 25000, 26 июня 2026", parse_mode='Markdown'); return INV_5
async def inv_5(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["work"] = p[0]; c.user_data["amount"] = p[1] if len(p)>1 else ""; c.user_data["date"] = p[2] if len(p)>2 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "invoice")

# ---- ДОГОВОР ----
async def contract_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/6* — Ваш телефон и email\n\nПример: +7 999 123-45-67, ivan@mail.ru\n_(или «-»)_", parse_mode='Markdown'); return CONTRACT_2
async def contract_2(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["exec_phone"] = "" if u.message.text=="-" else p[0]
    c.user_data["exec_email"] = p[1] if len(p)>1 else ""
    await u.message.reply_text("*Шаг 3/6* — Название заказчика", parse_mode='Markdown'); return CONTRACT_3
async def contract_3(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 4/6* — Что делаете? (предмет договора)", parse_mode='Markdown'); return CONTRACT_4
async def contract_4(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 5/6* — Стоимость и срок\n\nПример: 30000, 30 дней", parse_mode='Markdown'); return CONTRACT_5
async def contract_5(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = p[0]; c.user_data["deadline"] = p[1] if len(p)>1 else "по договорённости"
    await u.message.reply_text("*Шаг 6/6* — Город и дата подписания\n\nПример: Москва, 26 июня 2026", parse_mode='Markdown'); return CONTRACT_6
async def contract_6(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["city"] = p[0]; c.user_data["date"] = p[1] if len(p)>1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "contract")

# ---- ДОП. СОГЛАШЕНИЕ ----
async def addendum_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — Название заказчика", parse_mode='Markdown'); return ADDENDUM_2
async def addendum_2(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — Номер и дата основного договора\n\nПример: 12, 15 января 2026", parse_mode='Markdown'); return ADDENDUM_3
async def addendum_3(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["contract_num"] = p[0]; c.user_data["contract_date"] = p[1] if len(p)>1 else ""
    await u.message.reply_text("*Шаг 4/4* — В чём суть изменений?\n\nПример: Увеличение суммы до 50000 руб., продление срока на 2 недели", parse_mode='Markdown'); return ADDENDUM_4
async def addendum_4(u, c): c.user_data["work"] = u.message.text; return await send_pdf(u, c, "addendum")

# ---- КВИТАНЦИЯ ----
async def receipt_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — Плательщик (кто платит)", parse_mode='Markdown'); return RECEIPT_2
async def receipt_2(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — За что оплата?\n\nПример: Разработка логотипа", parse_mode='Markdown'); return RECEIPT_3
async def receipt_3(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 4/4* — Сумма и дата\n\nПример: 10000, 26 июня 2026", parse_mode='Markdown'); return RECEIPT_4
async def receipt_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["amount"] = p[0]; c.user_data["date"] = p[1] if len(p)>1 else datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "receipt")

# ---- ДОВЕРЕННОСТЬ ----
async def poa_1(u, c): c.user_data["grantor"] = u.message.text; await u.message.reply_text("*Шаг 2/4* — ФИО поверенного (кому доверяете)", parse_mode='Markdown'); return POA_2
async def poa_2(u, c): c.user_data["attorney"] = u.message.text; await u.message.reply_text("*Шаг 3/4* — Суть полномочий\n\nПример: подписание договоров, получение денег, представление в суде", parse_mode='Markdown'); return POA_3
async def poa_3(u, c): c.user_data["work"] = u.message.text; await u.message.reply_text("*Шаг 4/4* — Срок действия и город\n\nПример: 1 год, Москва", parse_mode='Markdown'); return POA_4
async def poa_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["validity"] = p[0]; c.user_data["city"] = p[1] if len(p)>1 else ""
    c.user_data["date"] = datetime.now().strftime('%d.%m.%Y')
    return await send_pdf(u, c, "poa")

# ---- КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ ----
async def cp_1(u, c): c.user_data["executor"] = u.message.text; await u.message.reply_text("*Шаг 2/5* — Телефон и email\n\nПример: +7 999 123-45-67, ivan@mail.ru\n_(или «-»)_", parse_mode='Markdown'); return CP_2
async def cp_2(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["exec_phone"] = "" if u.message.text=="-" else p[0]
    c.user_data["exec_email"] = p[1] if len(p)>1 else ""
    await u.message.reply_text("*Шаг 3/5* — Кому отправляем КП?\n\nПример: ООО «Ромашка»", parse_mode='Markdown'); return CP_3
async def cp_3(u, c): c.user_data["client"] = u.message.text; await u.message.reply_text("*Шаг 4/5* — Что предлагаете и стоимость\n\nПример: Разработка сайта под ключ, 80000", parse_mode='Markdown'); return CP_4
async def cp_4(u, c):
    p = [x.strip() for x in u.message.text.split(",")]
    c.user_data["work"] = p[0]; c.user_data["amount"] = p[1] if len(p)>1 else ""
    await u.message.reply_text("*Шаг 5/5* — Кратко о себе (2-3 слова)\n\nПример: 5 лет опыта, 200+ проектов", parse_mode='Markdown'); return CP_5
async def cp_5(u, c):
    c.user_data["about"] = u.message.text
    c.user_data["date"] = datetime.now().strftime('%d.%m.%Y')
    c.user_data["valid_until"] = "30 дней"
    return await send_pdf(u, c, "cp")

# ---- ОПЛАТА ----
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Написать @milorky", url="https://t.me/milorky")]])
    text = (
        "💳 *Подписка — 299 ₽/месяц*\n\n"
        "✅ Неограниченные документы\n"
        "✅ Все 7 типов документов\n"
        "✅ PDF с профессиональным дизайном\n"
        "✅ Скоро новые типы документов\n\n"
        "Оплата: СБП или карта.\n"
        "Активация в течение часа.\n\n"
        "_После оплаты напишите /myid и пришлите номер @milorky_"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш ID: `{update.effective_user.id}`\n\nОтправьте @milorky после оплаты.",
        parse_mode='Markdown')

async def activate_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ADMIN_USERNAME: return
    try:
        tid = int(context.args[0])
        get_user(tid)["paid"] = True
        await update.message.reply_text(f"✅ Подписка активирована для {tid}")
        await context.bot.send_message(chat_id=tid,
            text="🎉 *Подписка активирована!* Документов без ограничений. /new",
            parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /new — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query.data == "buy":
        await buy(update, context)

def main():
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    download_fonts()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_doc)],
        states={
            CHOOSE_DOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_doc)],
            ACT_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_1)],
            ACT_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_2)],
            ACT_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_3)],
            ACT_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_4)],
            ACT_5:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_5)],
            ACT_6:[MessageHandler(filters.TEXT & ~filters.COMMAND, act_6)],
            INV_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, inv_1)],
            INV_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, inv_2)],
            INV_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, inv_3)],
            INV_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, inv_4)],
            INV_5:[MessageHandler(filters.TEXT & ~filters.COMMAND, inv_5)],
            CONTRACT_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_1)],
            CONTRACT_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_2)],
            CONTRACT_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_3)],
            CONTRACT_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_4)],
            CONTRACT_5:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_5)],
            CONTRACT_6:[MessageHandler(filters.TEXT & ~filters.COMMAND, contract_6)],
            ADDENDUM_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_1)],
            ADDENDUM_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_2)],
            ADDENDUM_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_3)],
            ADDENDUM_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, addendum_4)],
            RECEIPT_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_1)],
            RECEIPT_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_2)],
            RECEIPT_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_3)],
            RECEIPT_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_4)],
            POA_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, poa_1)],
            POA_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, poa_2)],
            POA_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, poa_3)],
            POA_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, poa_4)],
            CP_1:[MessageHandler(filters.TEXT & ~filters.COMMAND, cp_1)],
            CP_2:[MessageHandler(filters.TEXT & ~filters.COMMAND, cp_2)],
            CP_3:[MessageHandler(filters.TEXT & ~filters.COMMAND, cp_3)],
            CP_4:[MessageHandler(filters.TEXT & ~filters.COMMAND, cp_4)],
            CP_5:[MessageHandler(filters.TEXT & ~filters.COMMAND, cp_5)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("activate_id", activate_id))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(conv)

    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
