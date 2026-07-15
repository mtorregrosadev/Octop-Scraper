import sys

# Configuració de preus (els mateixos que a l'scraper per coherència - ACTUALITZAT FEBRER 2026)
PRICE_PUNTA = 0.197
PRICE_LLANO = 0.118
PRICE_VALLE = 0.081

# Termes de Potència i Fixos (Per dia) - Segons factura real
POTENCIA_KW = 6.9
PEAJE_PUNTA_DIA = 0.097
PEAJE_VALLE_DIA = 0.027
BONO_SOCIAL_DIA = 0.019
ALQUILER_EQUIPOS_DIA = 0.027

IMPUESTO_ELECTRICO = 0.0511269  # 5.11%
IVA = 0.21  # 21%


def calcular_factura(kwh_punta, kwh_llano, kwh_valle, dies=30, potencia=POTENCIA_KW):
    # Consum Energia
    cost_consum = (
        (kwh_punta * PRICE_PUNTA)
        + (kwh_llano * PRICE_LLANO)
        + (kwh_valle * PRICE_VALLE)
    )

    # Potència (terme fix) + Altres conceptes fixos
    cost_potencia = (potencia * PEAJE_PUNTA_DIA * dies) + (
        potencia * PEAJE_VALLE_DIA * dies
    )
    cost_altres = (BONO_SOCIAL_DIA * dies) + (ALQUILER_EQUIPOS_DIA * dies)

    # Impost Elèctric (sobre consum + potència + altres conceptes relacionats)
    # Nota: El Bono Social i el Lloguer de vegades van després, però generalment el IE aplica al gruix.
    # En la factura: IE aplica sobre 94.46€ (que és Potencia + Energia + Bono + Alquiler? No exactament, cal veure la base).
    # Assumim base estàndard: (Energia + Potencia) * IE. Alquiler sol anar a part del IE, però sí porta IVA.
    # Simplificació ajustada a factura: IE s'aplica al subtotal energètic.
    base_ie = cost_consum + cost_potencia
    impost_e = base_ie * IMPUESTO_ELECTRICO

    total_base = base_ie + impost_e + cost_altres
    total_iva = total_base * IVA
    total_final = total_base + total_iva

    resum = (
        f"📊 **Càlcul Estimat de Factura ({dies} dies)**\n"
        f"----------------------------------\n"
        f"🔴 Punta: {kwh_punta:.2f} kWh -> {(kwh_punta*PRICE_PUNTA):.2f} €\n"
        f"🟡 Llano: {kwh_llano:.2f} kWh -> {(kwh_llano*PRICE_LLANO):.2f} €\n"
        f"🟢 Valle: {kwh_valle:.2f} kWh -> {(kwh_valle*PRICE_VALLE):.2f} €\n"
        f"🏠 Potència ({potencia} kW): {cost_potencia:.2f} €\n"
        f"🔌 Lloguer/Altres: {cost_altres:.2f} €\n"
        f"----------------------------------\n"
        f"💰 Subtotal: {total_base:.2f} €\n"
        f"📈 IVA ({int(IVA*100)}%): {total_iva:.2f} €\n"
        f"🚀 **TOTAL ESTIMAT: {total_final:.2f} €**\n"
    )
    return resum


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Ús: python3 calculadora.py PUNTA LLANO VALLE [DIES] [POTENCIA]")
    else:
        p = float(sys.argv[1])
        l = float(sys.argv[2])
        v = float(sys.argv[3])
        d = int(sys.argv[4]) if len(sys.argv) > 4 else 30
        pot = float(sys.argv[5]) if len(sys.argv) > 5 else POTENCIA_KW
        print(calcular_factura(p, l, v, d, pot))
