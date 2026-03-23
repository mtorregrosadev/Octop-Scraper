import asyncio
from datetime import date, timedelta, datetime
import sys
import os

# Afegim el directori actual al path per poder importar scraper
sys.path.append(os.getcwd())

try:
    from scraper import scrape_process, scrape_range
except ImportError:
    # Si falla, intentem importar com si estiguéssim al mateix directori
    try:
        from .scraper import scrape_process, scrape_range
    except ImportError:
        print("❌ No s'ha pogut importar 'scraper.py'. Assegura't de ser al directori correcte.")
        sys.exit(1)

# ---------------------------------------------------------
# CONFIGURACIÓ DE LA RECUPERACIÓ
# ---------------------------------------------------------
# Si vols canviar la data d'inici, modifica aquesta línia:
START_DATE = date(2026, 1, 1)
# ---------------------------------------------------------

async def main():
    print("🛠️  Eina de Recuperació d'Historial Octopus Energy")
    print("==================================================")
    
    # Demanar data opcionalment o fer servir la per defecte
    print(f"Data d'inici per defecte: {START_DATE}")
    print("Vols canviar-la? (Format YYYY-MM-DD) [Prem Enter per mantenir la per defecte]")
    user_input = input("> ").strip()
    
    start_date = START_DATE
    if user_input:
        try:
            start_date = datetime.strptime(user_input, "%Y-%m-%d").date()
        except ValueError:
            print("⚠️  Format incorrecte. Farem servir la data per defecte.")
    
    today = date.today()
    # Octopus sol tenir dades fins fa 2 dies (de vegades 1)
    end_date = today - timedelta(days=2)
    
    if start_date > end_date:
        print(f"❌ La data d'inici ({start_date}) és posterior a la data disponible ({end_date}).")
        return

    total_days = (end_date - start_date).days + 1
    print(f"\n🔄 Recuperant {total_days} dies: des de {start_date} fins a {end_date}")
    print("⚠️  Això pot trigar una estona. No tanquis la finestra.\n")
    
    # Demanem opcions de visualització
    headless = True
    print("\nVols veure el navegador mentre treballa? (s/n) [Per defecte: No]")
    if input("> ").strip().lower() == 's':
        headless = False

    # Executem el scrape_range
    # Per defecte silenciós fins avui (per no enviar SPAM de tot l'any), excepte si l'usuari volgués una altra cosa
    # Però per fer-ho genèric, posarem silent_until=end_date, així només recupera dades sense enviar res.
    # Si vols que t'envïi coses, pots modificar-ho aquí o fer una pregunta extra.
    
    # En el teu cas específic volies fins al 16 de març silenciós:
    silent_date = end_date
    print(f"\nFins a quina data vols que sigui SILENCIÓS (sense enviar Telegram)? Format YYYY-MM-DD [Per defecte: {end_date}]")
    s_input = input("> ").strip()
    if s_input:
        try: silent_date = datetime.strptime(s_input, "%Y-%m-%d").date()
        except: pass

    await scrape_range(start_date, end_date, silent_until=silent_date, headless=headless)
    
    print("\n✅ Procés finalitzat!")

if __name__ == "__main__":
    asyncio.run(main())
