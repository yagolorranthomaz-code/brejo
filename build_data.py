#!/usr/bin/env python3
"""
build_data.py -> games.json    (base completa do brejo.html)

POR QUE ESTE ARQUIVO EXISTE
  O navegador nao consegue ler o cdn.nba.com por causa de CORS. Mas um
  servidor consegue: nao existe CORS fora do navegador, e o CDN da NBA nao
  tem bloqueio de bot nem de IP de datacenter.

  Entao quem busca os jogos e este script, rodando no GitHub Actions uma vez
  por dia. Ele comita o games.json no repositorio, e o brejo.html so le esse
  arquivo - que fica na mesma origem, sem CORS nenhum.

  Resultado: ninguem roda nada na mao e ninguem sobe arquivo.

FONTES
  cdn.nba.com   calendario e box score de cada jogo (placar, minutos, DNP)
  Sleeper       nascimento, temporadas, altura, peso, faculdade
  GitHub        salario e anos de contrato (dataset publico do Spotrac)
  stats.nba.com draft e pais - opcional, costuma falhar em datacenter

USO
  pip install requests
  python build_data.py
"""

import concurrent.futures as cf
import csv
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Falta o requests. Rode:  pip install requests")

# ----------------------------------------------------------------------------
JOGOS_POR_TIME = 40         # profundidade da janela movel
OUT = "games.json"
THREADS = 8
TIMEOUT = 30

USE_SLEEPER = True
USE_SALARIOS = True
USE_STATS_NBA = True        # draft/pais; falha em datacenter, e tudo bem

CDN = "https://cdn.nba.com/static/json"
SCHEDULE_URLS = [
    f"{CDN}/staticData/scheduleLeagueV2_1.json",
    f"{CDN}/staticData/scheduleLeagueV2.json",
]
SALARIO_URL = ("https://raw.githubusercontent.com/gabriel1200/"
               "site_Data/master/salary.csv")

COLS = ["date", "tm", "loc", "opp", "res", "tmpts", "opppts", "mp",
        "fg", "fga", "fg3", "fg3a", "ft", "fta",
        "orb", "drb", "trb", "ast", "stl", "blk", "tov", "pf",
        "pts", "pm", "reason"]

SCORING = {"pts": 1, "trb": 1.2, "ast": 1.5, "stl": 3, "blk": 3,
           "tov": -1, "tech": -0.5}

SESSION = requests.Session()
# O edge da NBA (Akamai) barra User-Agent que nao parece navegador. Um UA de
# Chrome real passa nas rotas do cdn.nba.com (calendario e box scores).
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.nba.com/",
})


# ----------------------------------------------------------------------------
def chave_nome(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def num(v):
    if v in (None, "", "None"):
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 2)
    except (TypeError, ValueError):
        return None


def iso_min(s):
    """'PT34M12.00S' -> 34.2"""
    if not s:
        return None
    m = re.search(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", str(s))
    if not m:
        return None
    mi = int(m.group(1) or 0)
    se = float(m.group(2) or 0)
    return round(mi + se / 60, 2)


def idade_decimal(nasc):
    if not nasc:
        return None
    try:
        d = datetime.strptime(str(nasc)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return round((datetime.now() - d).days / 365.25, 1)


def data_do_jogo(g, dia):
    for v in (g.get("gameDateTimeEst"), g.get("gameDateEst"),
              g.get("gameDateTimeUTC"), g.get("gameTimeEt")):
        s = str(v or "")
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:10]
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", str(dia.get("gameDate") or ""))
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


# ----------------------------------------------------------------------------
def buscar_calendario():
    ultimo = None
    for u in SCHEDULE_URLS:
        try:
            r = SESSION.get(u, timeout=TIMEOUT)
            if r.status_code != 200:
                ultimo = f"HTTP {r.status_code}"
                continue
            j = r.json()
            dias = (j.get("leagueSchedule") or {}).get("gameDates") or j.get("gameDates")
            if dias:
                return dias
            ultimo = "formato inesperado"
        except Exception as e:  # noqa: BLE001
            ultimo = str(e)[:90]
    sys.exit(f"Nao consegui o calendario: {ultimo}")


def selecionar_jogos(dias, alvo):
    """Jogos encerrados e anteriores a hoje. Anda de tras pra frente ate cada
    time somar `alvo` partidas."""
    hoje = datetime.now().strftime("%Y-%m-%d")
    todos = []
    for dia in dias:
        for g in (dia.get("games") or []):
            dt = data_do_jogo(g, dia)
            if not dt or dt >= hoje:
                continue
            if g.get("gameStatus") and g["gameStatus"] != 3:
                continue
            todos.append({
                "id": str(g.get("gameId")), "data": dt,
                "h": (g.get("homeTeam") or {}).get("teamTricode") or "",
                "a": (g.get("awayTeam") or {}).get("teamTricode") or "",
            })
    todos.sort(key=lambda x: x["data"])

    conta, sel = {}, []
    for g in reversed(todos):
        ch, ca = conta.get(g["h"], 0), conta.get(g["a"], 0)
        if ch >= alvo and ca >= alvo:
            continue
        conta[g["h"]] = ch + 1
        conta[g["a"]] = ca + 1
        sel.append(g)
    sel.sort(key=lambda x: x["data"])
    return sel


def baixar_box(gid):
    try:
        r = SESSION.get(f"{CDN}/liveData/boxscore/boxscore_{gid}.json", timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def linhas_do_box(j):
    g = (j or {}).get("game") or {}
    if not g:
        return []
    data = str(g.get("gameTimeLocal") or g.get("gameEt") or g.get("gameTimeUTC") or "")[:10]
    out = []
    for meu, dele, loc in (("homeTeam", "awayTeam", "H"), ("awayTeam", "homeTeam", "A")):
        t, o = g.get(meu) or {}, g.get(dele) or {}
        if not t or not o:
            continue
        tp, op = t.get("score"), o.get("score")
        for pl in (t.get("players") or []):
            st = pl.get("statistics") or {}
            mp = iso_min(st.get("minutes"))
            rec = {
                "date": data, "tm": t.get("teamTricode") or "", "loc": loc,
                "opp": o.get("teamTricode") or "",
                "res": "W" if (tp or 0) > (op or 0) else ("L" if (tp or 0) < (op or 0) else ""),
                "tmpts": tp, "opppts": op, "mp": mp,
                "fg": num(st.get("fieldGoalsMade")), "fga": num(st.get("fieldGoalsAttempted")),
                "fg3": num(st.get("threePointersMade")), "fg3a": num(st.get("threePointersAttempted")),
                "ft": num(st.get("freeThrowsMade")), "fta": num(st.get("freeThrowsAttempted")),
                "orb": num(st.get("reboundsOffensive")), "drb": num(st.get("reboundsDefensive")),
                "trb": num(st.get("reboundsTotal")), "ast": num(st.get("assists")),
                "stl": num(st.get("steals")), "blk": num(st.get("blocks")),
                "tov": num(st.get("turnovers")), "pf": num(st.get("foulsPersonal")),
                "pts": num(st.get("points")), "pm": num(st.get("plusMinusPoints")),
                "reason": (pl.get("notPlayingReason") or "Nao jogou") if not mp else None,
            }
            nome = pl.get("name") or f"{pl.get('firstName','')} {pl.get('familyName','')}".strip()
            out.append({
                "pid": str(pl.get("personId")), "nome": nome,
                "time": t.get("teamTricode") or "", "pos": pl.get("position") or "",
                "linha": [rec.get(c) for c in COLS],
            })
    return out


# ----------------------------------------------------------------------------
def buscar_sleeper(jogadores):
    print("[3/5] Sleeper (nascimento, temporadas, altura)...")
    try:
        r = requests.get("https://api.sleeper.app/v1/players/nba", timeout=120)
        r.raise_for_status()
        bruto = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"      pulou: {str(e)[:80]}")
        return 0
    por_nome = {}
    for p in bruto.values():
        if not isinstance(p, dict):
            continue
        nome = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}"
        k = chave_nome(nome)
        if k:
            por_nome[k] = p
    n = 0
    for p in jogadores.values():
        sl = por_nome.get(chave_nome(p["name"]))
        if not sl:
            continue
        if sl.get("birth_date"):
            p["bio"]["born"] = sl["birth_date"]
            p["age"] = idade_decimal(sl["birth_date"])
        if sl.get("years_exp") is not None:
            p["bio"]["exp"] = num(sl["years_exp"])
        if sl.get("height"):
            p["bio"].setdefault("height", sl["height"])
        if sl.get("weight"):
            p["bio"].setdefault("weight", num(sl["weight"]))
        if sl.get("college"):
            p["bio"].setdefault("college", sl["college"])
        if sl.get("position") and not p["pos"]:
            p["pos"] = sl["position"]
        n += 1
    print(f"      {n} jogadores com bio")
    return n


def buscar_salarios(jogadores):
    print("[4/5] Salarios e contratos...")
    try:
        r = requests.get(SALARIO_URL, timeout=60)
        r.raise_for_status()
        linhas = list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:  # noqa: BLE001
        print(f"      pulou: {str(e)[:80]}")
        return 0
    if not linhas:
        return 0
    cols = sorted(c for c in linhas[0].keys() if re.match(r"^\d{4}-\d{2}$", c or ""))
    if not cols:
        print("      formato inesperado")
        return 0
    atual = cols[0]
    por_nome = {chave_nome(p["name"]): p for p in jogadores.values()}
    n = 0
    for x in linhas:
        p = por_nome.get(chave_nome(x.get("Player") or ""))
        if not p:
            continue
        sal = num(x.get(atual))
        anos = sum(1 for c in cols if (num(x.get(c)) or 0) > 0)
        if sal or anos:
            p["contract"] = {"salary": sal, "years": anos}
            n += 1
    print(f"      {n} com contrato (temporada {atual})")
    return n


def buscar_draft(jogadores):
    """Draft e pais. Sai do stats.nba.com, que costuma bloquear datacenter -
    se falhar, o resto continua igual."""
    print("[5/5] Draft e pais (opcional)...")
    try:
        temp = datetime.now()
        ano = temp.year if temp.month >= 9 else temp.year - 1
        season = f"{ano}-{str(ano + 1)[-2:]}"
        vazios = ("College Conference Country DateFrom DateTo Division DraftPick "
                  "DraftYear GameScope GameSegment Height LastNGames Location "
                  "Month OpponentTeamID Outcome PORound Period PlayerExperience "
                  "PlayerPosition SeasonSegment ShotClockRange StarterBench "
                  "VsConference VsDivision Weight").split()
        params = dict.fromkeys(vazios, "")
        params.update({"LeagueID": "00", "Season": season,
                       "SeasonType": "Regular Season", "PerMode": "PerGame",
                       "TeamID": "0", "LastNGames": "0", "Month": "0",
                       "OpponentTeamID": "0", "PORound": "0", "Period": "0"})
        r = requests.get("https://stats.nba.com/stats/leaguedashplayerbiostats",
                         params=params, timeout=30, headers={
                             "Host": "stats.nba.com",
                             "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                                            "Chrome/126.0.0.0 Safari/537.36"),
                             "Referer": "https://stats.nba.com/",
                             "Origin": "https://stats.nba.com",
                             "x-nba-stats-origin": "stats",
                             "x-nba-stats-token": "true",
                             "Accept": "application/json, text/plain, */*",
                         })
        if r.status_code != 200:
            print(f"      pulou: HTTP {r.status_code}")
            return 0
        s = r.json()["resultSets"][0]
        idx = {h: i for i, h in enumerate(s["headers"])}
        n = 0
        for linha in s["rowSet"]:
            pid = str(linha[idx["PLAYER_ID"]])
            p = jogadores.get(pid)
            if not p:
                continue
            dy = linha[idx.get("DRAFT_YEAR", 0)]
            if dy and str(dy) != "Undrafted":
                p["bio"]["draft"] = (f"{dy} | rodada {linha[idx['DRAFT_ROUND']]} "
                                     f"| pick {linha[idx['DRAFT_NUMBER']]}")
            elif dy:
                p["bio"]["draft"] = "Nao draftado"
            if linha[idx.get("COUNTRY", 0)]:
                p["bio"]["country"] = linha[idx["COUNTRY"]]
            n += 1
        print(f"      {n} enriquecidos")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"      pulou: {str(e)[:80]}")
        return 0


# ----------------------------------------------------------------------------
def main():
    print(f"Montando a base | janela de {JOGOS_POR_TIME} jogos por time\n")

    print("[1/5] Calendario...")
    dias = buscar_calendario()
    jogos = selecionar_jogos(dias, JOGOS_POR_TIME)
    if not jogos:
        sys.exit("O calendario nao trouxe jogos encerrados.")
    print(f"      {len(jogos)} jogos a baixar")

    print(f"[2/5] Box scores ({THREADS} em paralelo)...")
    boxes, falhas = [], 0
    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        futuros = {ex.submit(baixar_box, g["id"]): g for g in jogos}
        for i, fut in enumerate(cf.as_completed(futuros), 1):
            b = fut.result()
            if b:
                boxes.append(b)
            else:
                falhas += 1
            if i % 50 == 0 or i == len(jogos):
                print(f"      {i}/{len(jogos)}")
    if not boxes:
        sys.exit("Nenhum box score respondeu.")
    print(f"      {len(boxes)} baixados, {falhas} falharam")

    jogadores = {}
    i_data = COLS.index("date")
    for b in boxes:
        for x in linhas_do_box(b):
            p = jogadores.get(x["pid"])
            if not p:
                p = {"id": x["pid"], "name": x["nome"], "team": x["time"],
                     "pos": x["pos"], "age": None, "bio": {}, "g": []}
                jogadores[x["pid"]] = p
            if x["time"]:
                p["team"] = x["time"]
            if x["pos"] and not p["pos"]:
                p["pos"] = x["pos"]
            if any(r[i_data] == x["linha"][i_data] for r in p["g"]):
                continue
            p["g"].append(x["linha"])

    if USE_SLEEPER:
        buscar_sleeper(jogadores)
    if USE_SALARIOS:
        buscar_salarios(jogadores)
    if USE_STATS_NBA:
        buscar_draft(jogadores)

    lista = []
    for p in jogadores.values():
        p["g"].sort(key=lambda r: str(r[i_data]))
        if len(p["g"]) > JOGOS_POR_TIME:
            p["g"] = p["g"][-JOGOS_POR_TIME:]
        lista.append(p)
    lista.sort(key=lambda p: p["name"])

    ate = max((r[i_data] for p in lista for r in p["g"] if r[i_data]), default="")
    payload = {
        "cols": COLS,
        "scoring": SCORING,
        "meta": {
            "fonte": "cdn.nba.com",
            "jogos_por_jogador": JOGOS_POR_TIME,
            "jogos_baixados": len(boxes),
            "ate": ate,
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
        },
        "players": lista,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    mb = os.path.getsize(OUT) / 1e6
    print(f"\n{OUT} | {mb:.1f} MB | {len(lista)} jogadores | ate {ate}")


if __name__ == "__main__":
    main()
