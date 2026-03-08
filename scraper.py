import os
import asyncio
import re
import subprocess
import json
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
WORKSPACE = "/Users/mtorregrosadev/Documents/GitHub/octop-cons"
USER_DATA_DIR = os.path.join(WORKSPACE, "user_data")
HISTORY_FILE = os.path.join(WORKSPACE, "data_history.json")
CHART_PATH = os.path.join(WORKSPACE, "last_chart.png")
DETAILS_PATH = os.path.join(WORKSPACE, "detalle_consumo.txt")
ACCOUNT_ID = "A-8F2CFDED"
LOGIN_URL = "https://octopusenergy.es/login"
CONSUMO_URL = f"https://octopusenergy.es/dashboard/accounts/{ACCOUNT_ID}/explora-tu-consumo"

# Credenciales y Telegram
OCTOPUS_USER = os.getenv("OCTOPUS_USER")
OCTOPUS_PASS = os.getenv("OCTOPUS_PASS")
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-1003773743707")
TELEGRAM_THREAD_ID = "38"

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

def update_and_calculate(target_date_str, total_kwh, total_cost, stats, intervals):
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f: history = json.load(f)
        except: pass
    history[target_date_str] = {
        "kwh": total_kwh, "cost": total_cost,
        "desglose": {"PUNTA": stats["PUNTA"], "LLANO": stats["LLANO"], "VALLE": stats["VALLE"]},
        "intervals": intervals
    }
    with open(HISTORY_FILE, "w") as f: json.dump(history, f, indent=4)
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
    for h_str, kwh in intervals[-6:]:
        if kwh <= 0.0: gaps.append(h_str)
    return gaps

async def parse_table_data(page):
    try:
        await page.wait_for_selector('h4.efWsLj', timeout=15000)
        total_kwh_text = await page.inner_text('div.jciMSz')
        date_text = await page.inner_text('h4.efWsLj')
        body_text = await page.inner_text('body')
        matches = re.findall(r'(\d{2}:\d{2})\s+([0-9.,]+)\s*kWh', body_text)
        intervals = []
        if matches:
            for t, v in matches: intervals.append((t, float(v.replace(',', '.'))))
        return {"date": date_text, "total_web": total_kwh_text.strip(), "intervals": intervals}
    except: return None

async def send_telegram_report(dades):
    if not dades or not dades['intervals']: return False
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
    try:
        subprocess.run(["clawdbot", "message", "send", "-t", TELEGRAM_TARGET, "--thread-id", TELEGRAM_THREAD_ID, "-m", msg, "--media", CHART_PATH], check=True)
        subprocess.run(["clawdbot", "message", "send", "-t", TELEGRAM_TARGET, "--thread-id", TELEGRAM_THREAD_ID, "-m", "📄 Detalle horario adjunto", "--media", DETAILS_PATH], check=True)
        return True
    except: return False

async def handle_login(page):
    try:
        cookie_btn = page.locator('button:has-text("Aceptar")').first
        if await cookie_btn.is_visible(timeout=3000): await cookie_btn.click()
    except: pass
    if "login" in page.url and OCTOPUS_USER and OCTOPUS_PASS:
        await page.fill('input[name="email"]', OCTOPUS_USER); await page.fill('input[name="password"]', OCTOPUS_PASS)
        await page.locator('button:has-text("Iniciar sesión"), button[type="submit"]').first.click()
        await page.wait_for_url("**/dashboard**", timeout=60000)

async def scrape_process(specific_date=None):
    if not os.path.exists(USER_DATA_DIR): os.makedirs(USER_DATA_DIR)
    if specific_date:
        target_date = specific_date
    else:
        target_date = (datetime.now() - timedelta(days=2)).date()
    print(f"🎯 Iniciant procés de scraping per a: {target_date}")
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(user_data_dir=USER_DATA_DIR, headless=True)
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
            while True:
                current_screen_date_text = await page.inner_text('h4.efWsLj')
                current_screen_date = parse_date_octopus(current_screen_date_text)
                if not current_screen_date: await asyncio.sleep(10); continue
                
                diff = (target_date - current_screen_date.date()).days
                if diff == 0:
                    # Comprobación específica de "No data"
                    content_text = await page.inner_text('body')
                    if "No data" in content_text or "No hay datos" in content_text:
                        if not waiting_notified:
                            msg_wait = f"⏳ **Octopus aún no ha publicado los datos**\n\nPara el día {target_date.strftime('%d/%m/%Y')} aparece 'No data/No hay datos'.\n\n🔄 Quedo a la espera y lo enviaré en cuanto estén disponibles. 🐒💤"
                            subprocess.run(["clawdbot", "message", "send", "-t", TELEGRAM_TARGET, "--thread-id", TELEGRAM_THREAD_ID, "-m", msg_wait])
                            waiting_notified = True
                        await asyncio.sleep(1800); await page.reload(); await asyncio.sleep(5)
                        try: await table_btn.click(); await asyncio.sleep(2)
                        except: pass
                        continue

                    dades = await parse_table_data(page)
                    if dades:
                        zeros_found = check_zeros(dades['intervals'])
                        if not zeros_found:
                            if await send_telegram_report(dades): break
                        elif not waiting_notified:
                            gap_str = ", ".join(zeros_found)
                            msg_wait = f"⏳ **Octopus aún no ha publicado todos los datos**\n\nEl día {target_date.strftime('%d/%m/%Y')} todavía no está disponible por completo.\n\n🔍 **GAP detectado:** En las franjas **{gap_str}** el consumo marca 0 kWh.\n\n🔄 Me quedo esperando y refrescando la página cada 30 minutos hasta que Octopus actualice. 🐒💤"
                            subprocess.run(["clawdbot", "message", "send", "-t", TELEGRAM_TARGET, "--thread-id", TELEGRAM_THREAD_ID, "-m", msg_wait])
                            waiting_notified = True
                    await asyncio.sleep(1800); await page.reload(); await asyncio.sleep(5)
                    try: await table_btn.click(); await asyncio.sleep(2)
                    except: pass
                elif diff < 0: await page.locator('[data-testid*="daterange-previous-button"]').click(); await asyncio.sleep(2)
                elif diff > 0: await page.locator('[data-testid*="daterange-forward-button"]').click(); await asyncio.sleep(2)
        except Exception as e: print(f"❌ Error durant el scraping: {e}")
        await context.close()

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
    
    # Intenta interpretar l'argument com una data específica (YYYY-MM-DD)
    try:
        specific_date = datetime.strptime(mode, "%Y-%m-%d").date()
        print(f"🗓 Execució manual per a la data: {specific_date}")
        await scrape_process(specific_date)
        return
    except ValueError:
        pass

    if mode == "--auto":
        print("🤖 Mode Auto (Daemon) activat. Sistema intel·ligent de recuperació de dies.")
        last_run_check = None
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
                    await scrape_process(next_target)
                    await asyncio.sleep(10) # Comprovem de seguida si hi ha més dies pendents
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
            # Si el següent dia que falta és anterior o igual a l'objectiu standard, recuperem seqüencialment
            while next_missing <= target_standard:
                print(f"🔄 Recuperant dia pendent (GAP detectat): {next_missing}")
                await scrape_process(next_missing)
                next_missing += timedelta(days=1)
                if next_missing <= target_standard:
                    print("⏳ Esperant 10 segons abans del següent dia...")
                    await asyncio.sleep(10)
        else:
            # Si no hi ha historial, executem normal
            await scrape_process(target_standard)

if __name__ == "__main__":
    asyncio.run(run())
