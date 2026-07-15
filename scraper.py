import os
import asyncio
import re
import html
import json
import shutil
import time
import requests
from playwright.async_api import async_playwright
from dotenv import load_dotenv
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg') # Forçar backend no interactiu per evitar bloquejos al cron
import matplotlib.pyplot as plt
import holidays

# Cargar variables de entorno
load_dotenv()

# Configuración
# Usa el directori real del script per evitar desajustos entre còpies del repo.
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(WORKSPACE, "user_data")
HISTORY_FILE = os.path.join(WORKSPACE, "data_history.json")
CHART_PATH = os.path.join(WORKSPACE, "last_chart.png")
DETAILS_PATH = os.path.join(WORKSPACE, "detalle_consumo.txt")
ACCOUNT_ID = os.getenv("OCTOPUS_ACCOUNT_ID", "").strip()
LOGIN_URL = "https://octopusenergy.es/login"
CONSUMO_URL = f"https://octopusenergy.es/dashboard/accounts/{ACCOUNT_ID}/explora-tu-consumo"

# Credenciales y Telegram
OCTOPUS_USER = os.getenv("OCTOPUS_USER")
OCTOPUS_PASS = os.getenv("OCTOPUS_PASS")
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "").strip()
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
PENDING_REPORTS_FILE = os.path.join(WORKSPACE, "pending_reports.json")
SENT_REPORTS_FILE = os.path.join(WORKSPACE, "sent_reports.json")
PENDING_MEDIA_DIR = os.path.join(WORKSPACE, "pending_media")
WAITING_NOTIFICATION_FILE = os.path.join(WORKSPACE, "waiting_notification.json")

# Precios y Periodos (Actualizados 2026-02 con factura real)
PRICE_PUNTA = 0.197
PRICE_LLANO = 0.118
PRICE_VALLE = 0.081

# Costos Fijos Diaris (Sense IVA ni Impostos)
POTENCIA_KW = 6.9
PEAJE_PUNTA_DIA = 0.097
PEAJE_VALLE_DIA = 0.027
BONO_SOCIAL_DIA = 0.019
ALQUILER_EQUIPOS_DIA = 0.027

# Impuestos
IMPOST_ELECTRIC = 0.0511269
IVA = 0.21

ES_TO_NUM = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
}

MES_MAP = {v: k.capitalize() for k, v in ES_TO_NUM.items()}

async def find_current_date_on_page(page):
    """Cerca la data actual mostrada a la pàgina per múltiples mètodes."""
    # Mètode 1: h4 (original)
    try:
        h4_locs = page.locator('h4')
        cnt = await h4_locs.count()
        for i in range(cnt):
            t = await h4_locs.nth(i).inner_text()
            if parse_date_octopus(t):
                return t
    except: pass

    # Mètode 2: h1, h2, h3, p
    for tag in ['h1', 'h2', 'h3', 'p']:
        try:
            locs = page.locator(tag)
            cnt = await locs.count()
            for i in range(min(cnt, 30)):
                t = await locs.nth(i).inner_text()
                if parse_date_octopus(t):
                    return t
        except: pass

    # Mètode 3: regex sobre el body complet
    try:
        body_text = await page.inner_text('body')
        matches = re.findall(
            r'\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|'
            r'septiembre|octubre|noviembre|diciembre)\s+de\s+\d{4}',
            body_text, re.IGNORECASE
        )
        if matches:
            return matches[0]
    except: pass

    return ""

def get_period(date_obj):
    es_holidays = holidays.ES(years=date_obj.year)
    if date_obj.weekday() >= 5 or date_obj.date() in es_holidays:
        return "VALLE", PRICE_VALLE, "#4CAF50", "🟢"
    hour = date_obj.hour
    if 0 <= hour < 8: return "VALLE", PRICE_VALLE, "#4CAF50", "🟢"
    elif (10 <= hour < 14) or (18 <= hour < 22): return "PUNTA", PRICE_PUNTA, "#F44336", "🔴"
    else: return "LLANO", PRICE_LLANO, "#FFEB3B", "🟡"

def generate_visual_chart(dades):
    if not dades or not dades['intervals']: return False
    target_date = parse_date_octopus(dades['date'])
    hours_labels = [x[0] for x in dades['intervals']]
    values = [x[1] for x in dades['intervals']]
    colors = []
    for h_str in hours_labels:
        h = int(h_str.split(':')[0])
        dt_hour = target_date.replace(hour=h)
        colors.append(get_period(dt_hour)[2])
    plt.figure(figsize=(12, 6), facecolor='#100030')
    ax = plt.axes()
    ax.set_facecolor('#100030')
    bars = plt.bar(hours_labels, values, color=colors, edgecolor='white', linewidth=0.5)
    for bar in bars:
        height = bar.get_height()
        if height > 0:
            plt.text(bar.get_x() + bar.get_width()/2., height + 0.02,
                     f'{height:.2f}', ha='center', va='bottom', color='white', fontsize=8, fontweight='bold')
    plt.title(f"Consumo Eléctrico - {dades['date']}", color='white', fontsize=14, pad=20)
    plt.xticks(rotation=45, color='white')
    plt.yticks(color='white')
    plt.grid(axis='y', linestyle='--', alpha=0.3, color='gray')
    ax.spines['bottom'].set_color('white')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('white')
    plt.tight_layout()
    plt.savefig(CHART_PATH, dpi=150)
    plt.close()
    return True

def parse_date_octopus(date_str):
    try:
        clean_str = date_str.lower().replace(',', '').replace(' de ', ' ')
        parts = clean_str.split()
        d = int(parts[0]); m = ES_TO_NUM.get(parts[1], 1); y = int(parts[2])
        return datetime(y, m, d)
    except: return None

def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except:
            pass
    return default

def save_json_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def load_waiting_notification_state():
    state = load_json_file(WAITING_NOTIFICATION_FILE, {})
    value = state.get("date")
    try:
        target_date = datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None
    if not target_date:
        return None
    return target_date, state.get("reason", "no-data")

def save_waiting_notification_state(target_date, reason):
    save_json_file(
        WAITING_NOTIFICATION_FILE,
        {"date": target_date.isoformat(), "reason": reason},
    )

def clear_waiting_notification_date():
    try:
        os.remove(WAITING_NOTIFICATION_FILE)
    except FileNotFoundError:
        pass

def pending_sort_key(date_str):
    parsed = parse_date_octopus(date_str)
    return parsed.isoformat() if parsed else date_str

def snapshot_pending_media(target_date_str, payload):
    """Congela els adjunts de cada report perquè no se sobreescriguin entre dies."""
    payload = dict(payload)
    parsed = parse_date_octopus(target_date_str)
    date_slug = parsed.strftime("%Y-%m-%d") if parsed else re.sub(r"[^a-zA-Z0-9_-]+", "-", target_date_str)
    os.makedirs(PENDING_MEDIA_DIR, exist_ok=True)

    for key, suffix in (("chart", ".png"), ("details", ".txt")):
        source = payload.get(key)
        if not source or not os.path.isfile(source):
            payload[key] = None
            continue
        destination = os.path.join(PENDING_MEDIA_DIR, f"{date_slug}-{key}{suffix}")
        if os.path.abspath(source) != os.path.abspath(destination):
            shutil.copy2(source, destination)
        payload[key] = destination
    return payload

def queue_pending_report(target_date_str, payload):
    pending = load_json_file(PENDING_REPORTS_FILE, {})
    pending[target_date_str] = snapshot_pending_media(target_date_str, payload)
    ordered = {k: pending[k] for k in sorted(pending.keys(), key=pending_sort_key)}
    save_json_file(PENDING_REPORTS_FILE, ordered)

def remove_pending_report(target_date_str):
    pending = load_json_file(PENDING_REPORTS_FILE, {})
    if target_date_str in pending:
        payload = pending[target_date_str]
        pending.pop(target_date_str, None)
        save_json_file(PENDING_REPORTS_FILE, pending)
        for key in ("chart", "details"):
            path = payload.get(key) if isinstance(payload, dict) else None
            if not path:
                continue
            try:
                is_snapshot = os.path.commonpath([os.path.abspath(path), PENDING_MEDIA_DIR]) == PENDING_MEDIA_DIR
            except ValueError:
                is_snapshot = False
            if is_snapshot:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

def was_report_sent(target_date_str):
    sent = load_json_file(SENT_REPORTS_FILE, {})
    return bool(sent.get(target_date_str))

def mark_report_sent(target_date_str):
    sent = load_json_file(SENT_REPORTS_FILE, {})
    sent[target_date_str] = datetime.now().isoformat()
    ordered = {k: sent[k] for k in sorted(sent.keys(), key=pending_sort_key)}
    save_json_file(SENT_REPORTS_FILE, ordered)

def update_history_entry(target_date_str, total_kwh, total_cost, stats, intervals):
    history = load_json_file(HISTORY_FILE, {})
    history[target_date_str] = {
        "kwh": total_kwh, "cost": total_cost,
        "desglose": {"PUNTA": stats["PUNTA"], "LLANO": stats["LLANO"], "VALLE": stats["VALLE"]},
        "intervals": intervals
    }
    save_json_file(HISTORY_FILE, history)
    return history

def delete_history_entry(target_date_str):
    history = load_json_file(HISTORY_FILE, {})
    if target_date_str in history:
        history.pop(target_date_str, None)
        save_json_file(HISTORY_FILE, history)

def update_and_calculate(target_date_str, total_kwh, total_cost, stats, intervals):
    history = update_history_entry(target_date_str, total_kwh, total_cost, stats, intervals)
    target_date = parse_date_octopus(target_date_str)
    week_kwh = week_cost = month_kwh = month_cost = year_kwh = year_cost = 0.0
    month_accumulated = {"PUNTA": 0.0, "LLANO": 0.0, "VALLE": 0.0}
    start_of_week = target_date - timedelta(days=target_date.weekday())
    for date_s, data in history.items():
        curr_date = parse_date_octopus(date_s)
        if not curr_date: continue
        if curr_date.date() > target_date.date(): continue
        if curr_date.year == target_date.year:
            year_kwh += data["kwh"]; year_cost += data["cost"]
            if curr_date.month == target_date.month:
                month_kwh += data["kwh"]; month_cost += data["cost"]
                if "desglose" in data:
                    for p in ["PUNTA", "LLANO", "VALLE"]: month_accumulated[p] += data["desglose"][p]["kwh"]
            if curr_date >= start_of_week and curr_date <= target_date:
                week_kwh += data["kwh"]; week_cost += data["cost"]
    return (week_kwh, week_cost), (month_kwh, month_cost), (year_kwh, year_cost), month_accumulated

def check_zeros(intervals):
    if not intervals: return []
    gaps = []
    for h_str, kwh in intervals:
        if kwh <= 0.0: gaps.append(h_str)
    return gaps


class TelegramAPIError(RuntimeError):
    pass


def telegram_html(message_text):
    """Converteix el Markdown simple existent a HTML segur per a Telegram."""
    escaped = html.escape(str(message_text))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


def telegram_base_payload():
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramAPIError(
            "Falta TELEGRAM_BOT_TOKEN. Crea el bot amb @BotFather i afegeix el token al fitxer .env."
        )
    if not TELEGRAM_TARGET:
        raise TelegramAPIError("Falta TELEGRAM_TARGET al fitxer .env.")

    payload = {"chat_id": TELEGRAM_TARGET}
    if TELEGRAM_THREAD_ID and TELEGRAM_THREAD_ID != "0":
        payload["message_thread_id"] = TELEGRAM_THREAD_ID
    return payload


def telegram_api_call(method, data, files=None, max_attempts=3):
    """Crida directa a Telegram Bot API amb reintents per xarxa, 429 i errors 5xx."""
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last_error = None

    for attempt in range(1, max_attempts + 1):
        if files:
            for file_data in files.values():
                file_obj = file_data[1] if isinstance(file_data, tuple) else file_data
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)

        try:
            response = requests.post(url, data=data, files=files, timeout=(10, 60))
        except requests.RequestException as exc:
            last_error = TelegramAPIError(
                f"Error de xarxa contactant Telegram ({type(exc).__name__})"
            )
            retry_after = min(2 ** (attempt - 1), 5)
        else:
            try:
                body = response.json()
            except ValueError:
                body = {}

            if response.ok and body.get("ok"):
                return body.get("result")

            description = body.get("description") or f"HTTP {response.status_code}"
            last_error = TelegramAPIError(f"Telegram {method}: {description}")
            retry_after = body.get("parameters", {}).get("retry_after", 0)
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                raise last_error
            retry_after = max(float(retry_after or 0), min(2 ** (attempt - 1), 5))

        if attempt < max_attempts:
            time.sleep(retry_after)

    raise last_error or TelegramAPIError(f"Telegram {method}: error desconegut")


def send_telegram_message(message_text):
    payload = telegram_base_payload()
    payload.update({"text": telegram_html(message_text), "parse_mode": "HTML"})
    return telegram_api_call("sendMessage", payload)


def send_telegram_photo(photo_path, caption=None):
    if not photo_path or not os.path.isfile(photo_path):
        raise TelegramAPIError(f"No existeix la imatge a enviar: {photo_path}")

    payload = telegram_base_payload()
    formatted_caption = telegram_html(caption) if caption else None
    if formatted_caption and len(formatted_caption) > 1024:
        formatted_caption = None

    if formatted_caption:
        payload.update({"caption": formatted_caption, "parse_mode": "HTML"})
    with open(photo_path, "rb") as photo:
        result = telegram_api_call(
            "sendPhoto",
            payload,
            files={"photo": (os.path.basename(photo_path), photo, "image/png")},
        )

    if caption and not formatted_caption:
        send_telegram_message(caption)
    return result


def send_telegram_document(document_path, caption=None):
    if not document_path or not os.path.isfile(document_path):
        raise TelegramAPIError(f"No existeix el document a enviar: {document_path}")

    payload = telegram_base_payload()
    if caption:
        payload.update({"caption": telegram_html(caption), "parse_mode": "HTML"})
    with open(document_path, "rb") as document:
        return telegram_api_call(
            "sendDocument",
            payload,
            files={"document": (os.path.basename(document_path), document, "text/plain")},
        )


def send_telegram_report_payload(message_text, chart_path=None, details_path=None):
    if chart_path and os.path.isfile(chart_path):
        send_telegram_photo(chart_path, message_text)
    else:
        send_telegram_message(message_text)
    if details_path and os.path.isfile(details_path):
        send_telegram_document(details_path, "📄 **Detalle horario adjunto**")


def try_send_telegram_notification(message_text):
    try:
        send_telegram_message(message_text)
        return True
    except Exception as exc:
        print(f"⚠️ No s'ha pogut enviar l'avís a Telegram: {exc}")
        return False

async def parse_table_data(page):
    try:
        try:
            total_kwh_text = await page.inner_text('div.jciMSz')
        except:
            total_kwh_text = "0"

        date_text = await find_current_date_on_page(page)

        body_text = await page.inner_text('body')
        matches = re.findall(r'(\d{2}:\d{2})\s+([0-9.,]+)\s*kWh', body_text)
        intervals = []
        if matches:
            for t, v in matches: intervals.append((t, float(v.replace(',', '.'))))

        if not intervals:
            await asyncio.sleep(3)
            body_text = await page.inner_text('body')
            matches = re.findall(r'(\d{2}:\d{2})\s+([0-9.,]+)\s*kWh', body_text)
            if matches:
                for t, v in matches: intervals.append((t, float(v.replace(',', '.'))))

        return {"date": date_text, "total_web": total_kwh_text.strip(), "intervals": intervals}
    except Exception as e:
        print(f"Error parse_table_data: {e}")
        return None

def flush_pending_reports():
    pending = load_json_file(PENDING_REPORTS_FILE, {})
    if not pending:
        return True
    for date_str in sorted(pending.keys(), key=pending_sort_key):
        payload = pending[date_str]
        staged_chart = payload.get("chart")
        staged_details = payload.get("details")
        msg = payload.get("message")
        try:
            send_telegram_report_payload(msg, staged_chart, staged_details)
            mark_report_sent(date_str)
            remove_pending_report(date_str)
            print(f"📤 Pending enviat: {date_str}")
        except Exception as e:
            print(f"⚠️ Pending encara no enviable ({date_str}): {e}")
            return False
    return True

async def send_telegram_report(dades, silent=False):
    if not dades or not dades['intervals']: return False
    gaps = check_zeros(dades['intervals'])
    if gaps:
        print(f"⏳ Report retingut per GAP: {', '.join(gaps)}")
        return False
    if was_report_sent(dades['date']):
        print(f"⏭️ Report ja enviat prèviament per {dades['date']}, salto duplicat")
        remove_pending_report(dades['date'])
        return True
    target_date = parse_date_octopus(dades['date'])
    dia = target_date.day; mes_es = MES_MAP.get(target_date.month, "Mes"); any_val = target_date.year
    tag_mes = f"#{mes_es}_{any_val}"; tag_dia = f"#{dia}_{mes_es}_{any_val}"
    total_kwh_real = 0.0; total_cost = 0.0
    stats = {"PUNTA": {"kwh": 0.0, "cost": 0.0, "emoji": "🔴"}, "LLANO": {"kwh": 0.0, "cost": 0.0, "emoji": "🟡"}, "VALLE": {"kwh": 0.0, "cost": 0.0, "emoji": "🟢"}}
    ascii_table = [f"DETALLE HORARIO - {dades['date']}", "-" * 40]
    detailed_data = []
    for hora_str, kwh in dades['intervals']:
        h = int(hora_str.split(':')[0])
        p_name, price, color, emoji = get_period(target_date.replace(hour=h))
        cost = kwh * price; total_kwh_real += kwh; total_cost += cost
        stats[p_name]["kwh"] += kwh; stats[p_name]["cost"] += cost
        ascii_table.append(f"{hora_str} -> {kwh:.3f} kWh [{p_name}]")
        detailed_data.append({"hora": hora_str, "kwh": kwh, "periodo": p_name, "coste": cost})
    with open(DETAILS_PATH, "w", encoding="utf-8") as f: f.write("\n".join(ascii_table))
    week, month, year, month_breakdown = update_and_calculate(dades['date'], total_kwh_real, total_cost, stats, detailed_data)
    pic_hora, pic_val = max(dades['intervals'], key=lambda x: x[1])
    generate_visual_chart(dades)
    dies_factura = target_date.day
    cost_energia_sim = sum(month_breakdown[p] * pr for p, pr in [("PUNTA", PRICE_PUNTA), ("LLANO", PRICE_LLANO), ("VALLE", PRICE_VALLE)])
    cost_potencia = (POTENCIA_KW * PEAJE_PUNTA_DIA * dies_factura) + (POTENCIA_KW * PEAJE_VALLE_DIA * dies_factura)
    cost_altres = (BONO_SOCIAL_DIA + ALQUILER_EQUIPOS_DIA) * dies_factura
    subtotal = cost_energia_sim + cost_potencia + cost_altres
    impost_e = subtotal * IMPOST_ELECTRIC
    total_sim = (subtotal + impost_e) * (1 + IVA)
    msg = f"{tag_mes} {tag_dia}\n📦 **OCTOPUS ENERGY REPORT**\n📅 {dades['date']}\n\n🚀 **Pico:** {pic_val:.3f} kWh ({pic_hora})\n💰 **Coste día: {total_cost:.2f} €**\n📊 **Consumo día: {total_kwh_real:.3f} kWh**\n\n🧾 **Factura Simulada ({mes_es} - {dies_factura} dies):**\n⚡ Energia: {cost_energia_sim:.2f} €\n🔌 Potència/Fixos: {(cost_potencia + cost_altres):.2f} €\n🏛️ Impostos (IE+IVA): {(impost_e + (subtotal+impost_e)*IVA):.2f} €\n💸 **TOTAL: {total_sim:.2f} €**\n\n📈 **Acumulados (kWh):**\n"
    msg += f"🗓 Semana: {week[0]:.2f} kWh | {week[1]:.2f} €\n📅 Mes: {month[0]:.2f} kWh | {month[1]:.2f} €\n🏢 Año: {year[0]:.2f} kWh | {year[1]:.2f} €\n\n✨ **Desglose hoy (Consumo | Coste):**\n"
    for p in ["PUNTA", "LLANO", "VALLE"]: msg += f"{stats[p]['emoji']} **{p}**: {stats[p]['kwh']:.2f} kWh | {stats[p]['cost']:.2f} €\n"
    
    chart_path = CHART_PATH if os.path.isfile(CHART_PATH) else None
    details_path = DETAILS_PATH if os.path.isfile(DETAILS_PATH) else None

    if silent:
        queue_pending_report(dades['date'], {"message": msg, "chart": chart_path, "details": details_path, "silent": True})
        print(f"✅ Dades guardades per {dades['date']} (Mode Silenciós)")
        return True

    flush_pending_reports()

    try:
        send_telegram_report_payload(msg, chart_path, details_path)
        mark_report_sent(dades['date'])
        remove_pending_report(dades['date'])
        return True
    except Exception as e:
        print(f"❌ Error enviant report directament a Telegram: {e}")
        queue_pending_report(dades['date'], {"message": msg, "chart": chart_path, "details": details_path, "silent": False, "error": str(e)})
        delete_history_entry(dades['date'])
        return False

async def send_telegram_error(error_msg):
    try:
        clean_error = str(error_msg)
        msg = f"⚠️ **Error Octop-Scraper**\n\n`{clean_error}`"
        send_telegram_message(msg)
    except Exception as exc:
        print(f"⚠️ No s'ha pogut notificar l'error a Telegram: {exc}")

async def handle_login(page):
    try:
        cookie_btn = page.locator('button:has-text("Aceptar")').first
        if await cookie_btn.is_visible(timeout=3000): await cookie_btn.click()
    except:
        pass

    if "login" in page.url and OCTOPUS_USER and OCTOPUS_PASS:
        await page.fill('input[name="email"]', OCTOPUS_USER)
        await page.fill('input[name="password"]', OCTOPUS_PASS)
        await page.locator('button:has-text("Iniciar sesión"), button[type="submit"]').first.click()

        # Octopus ara sovint deixa primer a /dashboard/accounts ("Elige una cuenta")
        # abans d'entrar a la vista concreta de consum. Acceptem qualsevol dashboard.
        await page.wait_for_url("**/dashboard/**", timeout=60000)

async def scrape_process(specific_date=None, silent=False, headless=True):
    if not os.path.exists(USER_DATA_DIR): os.makedirs(USER_DATA_DIR)
    if specific_date:
        target_date = specific_date
    else:
        target_date = (datetime.now() - timedelta(days=2)).date()
    print(f"🎯 Iniciant procés de scraping per a: {target_date}")
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(user_data_dir=USER_DATA_DIR, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        # Afegim timeout a la càrrega inicial
        try:
            await page.goto(LOGIN_URL, timeout=60000); await asyncio.sleep(2); await handle_login(page)
            await page.goto(CONSUMO_URL, timeout=60000); await asyncio.sleep(5)
            
            await page.locator('button:has-text("Día")').first.click(); await asyncio.sleep(2)
            table_btn = page.locator('button[aria-label*="table"], button[aria-label*="lista"]').first
            if not await table_btn.is_visible(): table_btn = page.locator('div[data-part="toggle-button-option-group"] button').last
            await table_btn.click(); await asyncio.sleep(3)
            
            waiting_notified = False
            no_date_counter = 0
            while True:
                current_screen_date_text = await find_current_date_on_page(page)

                if not current_screen_date_text:
                    no_date_counter += 1
                    if no_date_counter >= 4:  # ~8s sense data → probablement dia sense dades
                        print(f"  ⚠️ Sense data visible a la pàgina, navegant enrere...")
                        try:
                            await page.locator('[data-testid="chevron-left"]').first.click(force=True)
                        except: pass
                        no_date_counter = 0
                    await asyncio.sleep(2)
                    continue

                no_date_counter = 0
                current_screen_date = parse_date_octopus(current_screen_date_text)
                if not current_screen_date: await asyncio.sleep(10); continue
                
                diff = (target_date - current_screen_date.date()).days
                if diff == 0:
                    # Comprobación específica de "No data"
                    content_text = await page.inner_text('body')
                    if "No data" in content_text or "No hay datos" in content_text:
                        if not waiting_notified:
                            msg_wait = f"⏳ **Octopus aún no ha publicado los datos**\n\nPara el día {target_date.strftime('%d/%m/%Y')} aparece 'No data/No hay datos'.\n\n🔄 Quedo a la espera y lo enviaré en cuanto estén disponibles. 🐒💤"
                            waiting_notified = try_send_telegram_notification(msg_wait)
                        await asyncio.sleep(1800); await page.reload(); await asyncio.sleep(5)
                        try: await table_btn.click(); await asyncio.sleep(2)
                        except: pass
                        continue

                    dades = await parse_table_data(page)
                    if dades:
                        zeros_found = check_zeros(dades['intervals'])
                        if not zeros_found:
                            if await send_telegram_report(dades, silent=silent): break
                        elif not waiting_notified:
                            gap_str = ", ".join(zeros_found)
                            msg_wait = f"⏳ **Octopus aún no ha publicado todos los datos**\n\nEl día {target_date.strftime('%d/%m/%Y')} todavía no está disponible por completo.\n\n🔍 **GAP detectado:** En las franjas **{gap_str}** el consumo marca 0 kWh.\n\n🔄 Me quedo esperando y refrescando la página cada 30 minutos hasta que Octopus actualice. 🐒💤"
                            waiting_notified = try_send_telegram_notification(msg_wait)
                    await asyncio.sleep(1800); await page.reload(); await asyncio.sleep(5)
                    try: await table_btn.click(); await asyncio.sleep(2)
                    except: pass
                elif diff < 0: await page.locator('[data-testid="chevron-left"]').first.click(force=True); await asyncio.sleep(2)
                elif diff > 0: await page.locator('[data-testid="chevron-right"]').first.click(force=True); await asyncio.sleep(2)
        except Exception as e: 
            print(f"❌ Error durant el scraping: {e}")
            await send_telegram_error(e)
        await context.close()

async def scrape_range(start_date, end_date, silent_until=None, headless=True):
    if not os.path.exists(USER_DATA_DIR): os.makedirs(USER_DATA_DIR)
    print(f"🎯 Iniciant recuperació per rang: {start_date} -> {end_date}")
    missing_target = None
    missing_gaps = []
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(user_data_dir=USER_DATA_DIR, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        
        try:
            await page.goto(LOGIN_URL, timeout=60000); await asyncio.sleep(2); await handle_login(page)
            await page.goto(CONSUMO_URL, timeout=60000); await asyncio.sleep(5)
            
            # Switch to list view
            await page.locator('button:has-text("Día")').first.click(); await asyncio.sleep(2)
            table_btn = page.locator('button[aria-label*="table"], button[aria-label*="lista"]').first
            if not await table_btn.is_visible(): table_btn = page.locator('div[data-part="toggle-button-option-group"] button').last
            await table_btn.click(); await asyncio.sleep(3)

            current_target = start_date
            
            while current_target <= end_date:
                print(f"🔄 Processant: {current_target}")
                
                # Navigation loop to reach current_target
                no_date_nav_counter = 0
                while True:
                    current_screen_date_text = await find_current_date_on_page(page)

                    if not current_screen_date_text:
                        no_date_nav_counter += 1
                        if no_date_nav_counter >= 4:  # ~8s sense data → dia recent sense dades, anar enrere
                            print(f"  ⚠️ [{current_target}] Sense data a la pàgina, navegant enrere...")
                            try:
                                await page.locator('[data-testid="chevron-left"]').first.click(force=True)
                            except: pass
                            no_date_nav_counter = 0
                        await asyncio.sleep(2)
                        continue

                    no_date_nav_counter = 0
                    current_screen_date = parse_date_octopus(current_screen_date_text)
                    if not current_screen_date: await asyncio.sleep(2); continue

                    diff = (current_target - current_screen_date.date()).days

                    if diff == 0:
                        # We are at the target date
                        content_text = await page.inner_text('body')
                        if "No data" in content_text or "No hay datos" in content_text:
                            print(f"⏳ Sense dades publicades per {current_target}")
                            missing_target = current_target
                            break

                        dades = await parse_table_data(page)
                        if dades and dades['intervals']:
                            missing_gaps = check_zeros(dades['intervals'])
                            if missing_gaps:
                                print(
                                    f"⏳ GAP detectat per {current_target}: "
                                    f"{', '.join(missing_gaps)}"
                                )
                                missing_target = current_target
                            else:
                                is_silent = False
                                if silent_until and current_target <= silent_until:
                                    is_silent = True

                                # En mode rang, si tenim dades OK, les guardem/enviem
                                sent = await send_telegram_report(dades, silent=is_silent)
                                if not sent:
                                    missing_target = current_target
                        else:
                            print(f"⏳ Encara no hi ha intervals disponibles per {current_target}")
                            missing_target = current_target

                        # Move to next day in the outer loop
                        break
                        
                    elif diff < 0:
                         # Target is in the past relative to screen (screen is future) -> Go Prev
                         await page.locator('[data-testid="chevron-left"]').first.click(force=True); await asyncio.sleep(1.5)
                    elif diff > 0:
                         # Target is in the future relative to screen (screen is past) -> Go Next
                         await page.locator('[data-testid="chevron-right"]').first.click(force=True); await asyncio.sleep(1.5)
                
                if missing_target:
                    break

                # Advance target
                current_target += timedelta(days=1)
                
        except Exception as e: 
            print(f"❌ Error durant el scraping: {e}")
            await send_telegram_error(e)
            missing_target = current_target
        await context.close()
    return missing_target, missing_gaps

def get_last_stored_date():
    if not os.path.exists(HISTORY_FILE): return None
    try:
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
            if not history: return None
            dates = []
            for d_str in history.keys():
                parsed = parse_date_octopus(d_str)
                if parsed:
                    dates.append(parsed.date())
                    continue
                try: dates.append(datetime.strptime(d_str, "%d/%m/%Y").date())
                except:
                    try: dates.append(datetime.strptime(d_str, "%Y-%m-%d").date())
                    except: pass
            if not dates: return None
            return max(dates)
    except: return None

async def run():
    import sys
    mode = "daily"
    if len(sys.argv) > 1: mode = sys.argv[1]

    if mode == "--test-telegram":
        try:
            result = send_telegram_message(
                "✅ **Octop-Cons connectat directament a Telegram**\n\n"
                "La missatgeria funciona sense passarel·les externes."
            )
            print(f"✅ Telegram Bot API funciona (message_id={result.get('message_id', '?')})")
        except Exception as exc:
            print(f"❌ Prova de Telegram fallida: {exc}")
            raise SystemExit(2)
        return

    if mode == "--flush-pending":
        if not flush_pending_reports():
            raise SystemExit(2)
        print("✅ Cua pendent enviada a Telegram")
        return

    if not TELEGRAM_BOT_TOKEN:
        print("❌ Falta TELEGRAM_BOT_TOKEN al fitxer .env; no s'inicia el scraper.")
        raise SystemExit(2)
    if not ACCOUNT_ID:
        print("❌ Falta OCTOPUS_ACCOUNT_ID al fitxer .env; no s'inicia el scraper.")
        raise SystemExit(2)

    # Flag --visible pot aparèixer en qualsevol posició dels arguments
    headless = "--visible" not in sys.argv
    if not headless:
        print("👁️ Mode VISIBLE activat: el navegador serà visible.")

    if mode == "--range" and len(sys.argv) >= 3:
        # Recollim els args filtrant --visible
        date_args = [a for a in sys.argv[2:] if not a.startswith('--')]
        start_str = date_args[0] if len(date_args) >= 1 else None
        end_str = date_args[1] if len(date_args) >= 2 else datetime.now().strftime("%Y-%m-%d")
        if not start_str:
            print("❌ Cal especificar data d'inici: --range YYYY-MM-DD [YYYY-MM-DD]")
            return
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        print(f"🗓 Mode RANG activat: {start_date} -> {end_date}")
        await scrape_range(start_date, end_date, headless=headless)
        return
    
    # Intenta interpretar l'argument com una data específica (YYYY-MM-DD)
    try:
        specific_date = datetime.strptime(mode, "%Y-%m-%d").date()
        print(f"🗓 Execució manual per a la data: {specific_date}")
        await scrape_process(specific_date, headless=headless)
        return
    except ValueError:
        pass

    if mode == "--auto":
        print("🤖 Mode Auto (Daemon) activat. Sistema intel·ligent de recuperació de dies.")
        last_run_check = None
        waiting_notification_state = load_waiting_notification_state()
        while True:
            now = datetime.now()
            
            # 1. Recuperació intel·ligent basada en historial
            last_stored = get_last_stored_date()
            if last_stored:
                next_target = last_stored + timedelta(days=1)
                # Es pot processar si ara és >= (next_target + 2 dies a les 19:55)
                triggers_at = datetime.combine(next_target + timedelta(days=2), datetime.min.time()) + timedelta(hours=19, minutes=55)
                
                if now >= triggers_at:
                    print(f"🔄 Recuperant dia pendent: {next_target}")
                    missing_date, gap_hours = await scrape_range(
                        next_target,
                        (now - timedelta(days=2)).date(),
                    )
                    if missing_date:
                        reason = f"gap:{','.join(gap_hours)}" if gap_hours else "no-data"
                        notification_state = (missing_date, reason)
                        if waiting_notification_state != notification_state:
                            if gap_hours:
                                msg_wait = (
                                    "⏳ **GAP detectat a les dades d'Octopus**\n\n"
                                    f"El dia {missing_date.strftime('%d/%m/%Y')} conté "
                                    f"0 kWh a les franges **{', '.join(gap_hours)}**.\n\n"
                                    "📭 No enviaré el report fins que totes les franges tinguin dades. "
                                    "Ho tornaré a provar d'aquí a 30 minuts."
                                )
                            else:
                                msg_wait = (
                                    "⏳ **Octopus encara no ha publicat les dades**\n\n"
                                    f"El dia {missing_date.strftime('%d/%m/%Y')} continua mostrant "
                                    "'No data' o no conté intervals.\n\n"
                                    "🔄 Ho tornaré a provar automàticament d'aquí a 30 minuts."
                                )
                            if try_send_telegram_notification(msg_wait):
                                waiting_notification_state = notification_state
                                save_waiting_notification_state(missing_date, reason)
                        await asyncio.sleep(1800)
                    else:
                        waiting_notification_state = None
                        clear_waiting_notification_date()
                        await asyncio.sleep(10)  # Comprovem de seguida si hi ha més dies pendents
                    continue

            # 2. Fallback: Cron clàssic (si no hi ha historial o estem al corrent)
            if now.hour == 19 and now.minute == 55:
                today_str = now.strftime("%Y-%m-%d")
                if last_run_check != today_str:
                    target_standard = (now - timedelta(days=2)).date()
                    # Si ja tenim aquesta data (o posterior) a l'historial, no cal fer res
                    if last_stored and last_stored >= target_standard:
                        pass 
                    else:
                        await scrape_process()
                    last_run_check = today_str
            
            await asyncio.sleep(60)
    else:
        # Mode per defecte (sense arguments o argument no reconegut)
        # Abans d'executar el dia standard (fa 2 dies), revisem si hi ha forats a l'historial
        target_standard = (datetime.now() - timedelta(days=2)).date()
        last_stored = get_last_stored_date()
        
        if last_stored:
            next_missing = last_stored + timedelta(days=1)
            # Si el següent dia que falta és anterior o igual a l'objectiu standard, recuperem en un sol rang
            if next_missing <= target_standard:
                print(f"🔄 Recuperant GAPs en mode RANG: {next_missing} -> {target_standard}")
                await scrape_range(next_missing, target_standard, headless=headless)
            else:
                await scrape_process(target_standard, headless=headless)
        else:
            # Si no hi ha historial, executem normal
            await scrape_process(target_standard, headless=headless)

if __name__ == "__main__":
    asyncio.run(run())
