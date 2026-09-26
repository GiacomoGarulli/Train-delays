import os, time, requests
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
BASE = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
PROVA = os.environ.get("PROVA", "").strip()

# ======================= CONFIGURAZIONE =======================
STAZIONI = {
    "MONTEVARCHI": ("MONTEVARCHI", "MONTEVARCHI"),
    "SMN": ("FIRENZE", "NOVELLA"),
    "STATUTO": ("FIRENZE", "STATUTO"),
}

# 0=lun 1=mar 2=mer 3=gio 4=ven
# (partenza, orario, dove scendi); le alternative "o" vanno nello stesso gruppo
VIAGGI = {
    0: {"andata":  [("MONTEVARCHI", "07:06", "STATUTO")],
        "ritorno": [("SMN", "17:14", "MONTEVARCHI")]},
    3: {"andata":  [("MONTEVARCHI", "08:39", "SMN"),
                    ("MONTEVARCHI", "09:07", "SMN")],
        "ritorno": [("STATUTO", "18:47", "MONTEVARCHI"),
                    ("SMN", "19:14", "MONTEVARCHI")]},
    4: {"andata":  [("MONTEVARCHI", "07:06", "STATUTO")],
        "ritorno": [("SMN", "16:14", "MONTEVARCHI"),
                    ("STATUTO", "16:47", "MONTEVARCHI")]},
}

GIORNI_ESCLUSI = []   # es. ["2026-12-08", "2026-12-24"]
DATA_FINE = None      # es. "2027-01-31" -> dopo questa data non parte più

SOGLIA_VARIAZIONE = 2   # notifica solo se il ritardo cambia di almeno 2 min
SOGLIA_URGENTE = 10     # da 10 min in su: notifica ad alta priorità
INTERVALLO_SEC = 120    # controlla ogni 2 minuti
# ==============================================================


def invia(titolo, testo, priorita="default", tag="train"):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=testo.encode("utf-8"),
                      headers={"Title": titolo, "Priority": priorita, "Tags": tag},
                      timeout=10)
    except Exception as e:
        print("Errore invio notifica:", e)


SESSIONE = requests.Session()
SESSIONE.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "http://www.viaggiatreno.it/",
})


def get(path):
    for tentativo in range(3):
        r = SESSIONE.get(f"{BASE}/{path}", timeout=15)
        if r.status_code != 403:
            r.raise_for_status()
            return r
        print(f"403 da ViaggiaTreno, riprovo tra 10 secondi ({tentativo + 1}/3)")
        time.sleep(10)
    r.raise_for_status()


def ora(ms):
    return datetime.fromtimestamp(ms / 1000, TZ)


def codice_stazione(ricerca, parola):
    righe = get(f"autocompletaStazione/{quote(ricerca)}").text.strip().splitlines()
    for riga in righe:
        nome, codice = riga.split("|")
        if parola.upper() in nome.upper():
            return codice
    trovate = [r.split("|")[0] for r in righe]
    raise ValueError(f"Nessuna stazione con '{parola}' cercando '{ricerca}'. Trovate: {trovate}")

def andamento(cod_origine, numero, data_ms):
    return get(f"andamentoTreno/{cod_origine}/{numero}/{data_ms}").json()


def trova_treno(cod_part, orario, cod_arr):
    h, m = map(int, orario.split(":"))
    quando = datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(minutes=1)
    data_str = quando.strftime("%a %b %d %Y %H:%M:%S GMT%z")
    for p in get(f"partenze/{cod_part}/{quote(data_str)}").json():
        if p.get("compOrarioPartenza") != orario:
            continue
        a = andamento(p["codOrigine"], p["numeroTreno"], p["dataPartenzaTreno"])
        ids = [f["id"] for f in a["fermate"]]
        if cod_part in ids and cod_arr in ids and ids.index(cod_arr) > ids.index(cod_part):
            return p["codOrigine"], p["numeroTreno"], p["dataPartenzaTreno"]
    return None


def gruppo_di_adesso():
    adesso = datetime.now(TZ)
    oggi = adesso.date().isoformat()
    if oggi in GIORNI_ESCLUSI or (DATA_FINE and oggi > DATA_FINE):
        return []
    for gruppo in VIAGGI.get(adesso.weekday(), {}).values():
        prima = min(orario for _, orario, _ in gruppo)
        h, m = map(int, prima.split(":"))
        p = adesso.replace(hour=h, minute=m, second=0, microsecond=0)
        if p - timedelta(minutes=45) <= adesso <= p + timedelta(minutes=5):
            return gruppo
    return []


def prepara(gruppo):
    treni = []
    for da, orario, a in gruppo:
        titolo = f"{orario} {da} > {a}"
        cod_part = codice_stazione(*STAZIONI[da])
        cod_arr = codice_stazione(*STAZIONI[a])
        trovato = trova_treno(cod_part, orario, cod_arr)
        if not trovato:
            invia(titolo, "Non trovo questo treno su ViaggiaTreno: potrebbe essere "
                          "cancellato o l'orario potrebbe essere cambiato.", "high", "warning")
            continue
        cod_or, numero, data_ms = trovato
        h, m = map(int, orario.split(":"))
        treni.append({"titolo": titolo, "da": da, "a": a,
                      "cod_part": cod_part, "cod_arr": cod_arr,
                      "cod_origine": cod_or, "numero": numero, "data_ms": data_ms,
                      "partenza": datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0),
                      "ultimo": None, "avviso": "", "errori": 0})
    return treni


def segui(treni):
    if not treni:
        return
    fine = max(t["partenza"] for t in treni) + timedelta(hours=2, minutes=30)
    while treni and datetime.now(TZ) < fine:
        for t in list(treni):
            try:
                a = andamento(t["cod_origine"], t["numero"], t["data_ms"])
                t["errori"] = 0
                fermate = {f["id"]: f for f in a["fermate"]}
                fp, fa = fermate[t["cod_part"]], fermate[t["cod_arr"]]

                if fa.get("arrivoReale"):
                    invia(t["titolo"], f"Arrivato a {t['a']} alle {ora(fa['arrivoReale']):%H:%M}",
                          "low", "white_check_mark")
                    treni.remove(t)
                    continue

                ritardo = a.get("ritardo") or 0
                avviso = a.get("subTitle") or ""
                cambiato = (t["ultimo"] is None
                            or abs(ritardo - t["ultimo"]) >= SOGLIA_VARIAZIONE
                            or avviso != t["avviso"])
                if not cambiato:
                    continue

                righe = [f"Treno {t['numero']} - ritardo {ritardo} min"]
                if not fp.get("partenzaReale") and fp.get("partenza_teorica"):
                    part_prev = ora(fp["partenza_teorica"]) + timedelta(minutes=ritardo)
                    righe.append(f"Partenza da {t['da']}: {part_prev:%H:%M}")
                arr_teo = ora(fa["arrivo_teorico"])
                arr_prev = arr_teo + timedelta(minutes=ritardo)
                righe.append(f"Arrivo a {t['a']}: {arr_prev:%H:%M} (orario {arr_teo:%H:%M})")
                ril = a.get("stazioneUltimoRilevamento")
                if ril and ril != "--":
                    riga = f"Ultimo rilevamento: {ril}"
                    if a.get("oraUltimoRilevamento"):
                        riga += f" alle {ora(a['oraUltimoRilevamento']):%H:%M}"
                    righe.append(riga)
                if avviso:
                    righe.append(f"⚠️ {avviso}")

                urgente = ritardo >= SOGLIA_URGENTE or bool(avviso)
                invia(t["titolo"], "\n".join(righe),
                      "high" if urgente else "default", "warning" if urgente else "train")
                t["ultimo"], t["avviso"] = ritardo, avviso

            except Exception as e:
                t["errori"] += 1
                print("Errore:", t["titolo"], e)
                if t["errori"] == 5:
                    invia(t["titolo"], "Non riesco a leggere i dati da ViaggiaTreno da 10 minuti: "
                                       "controlla manualmente.", "high", "warning")
        time.sleep(INTERVALLO_SEC)


def main():
    if PROVA:
        da, orario, a = PROVA.split()
        gruppo = [(da.upper(), orario, a.upper())]
    else:
        gruppo = gruppo_di_adesso()
    if not gruppo:
        print("Nessun treno da seguire adesso.")
        return
    segui(prepara(gruppo))


if __name__ == "__main__":
    main()