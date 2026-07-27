#!/usr/bin/env python3
"""
build_data.py -> games.json    (base do brejo.html)

POR QUE A FONTE MUDOU
  O cdn.nba.com passou a devolver 403/Access Denied - inclusive em navegador
  residencial. E protecao por sessao do Akamai, entao nem trocar de IP nem
  ajustar User-Agent resolve. O stats.nba.com bloqueia IP de datacenter.

  A fonte atual e um dataset publico no GitHub, alimentado por um workflow
  que roda todo dia as 14h UTC. Caracteristicas verificadas:
    - responde access-control-allow-origin: *
    - uma linha por jogador por jogo, com data, GAME_ID e o box score inteiro
    - sem Cloudflare, sem Akamai, sem bloqueio de datacenter

  Se um dia esse repositorio parar, o unico ajuste e a constante GAMES_URL.

USO
  pip install requests
  python build_data.py
"""

import csv
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Falta o requests. Rode:  pip install requests")

# ----------------------------------------------------------------------------
JOGOS_POR_TIME = 40        # janela movel por jogador
OUT = "games.json"
TIMEOUT = 300

# No dataset, o ano e o final da temporada: 2026 = temporada 2025-26.
SEASON = 0                 # 0 = detecta sozinho

RAW = "https://raw.githubusercontent.com"
GAMES_URL = RAW + "/gabriel1200/player_sheets/master/game_report/all_games/all_{ano}.csv"
SALARIO_URL = RAW + "/gabriel1200/site_Data/master/salary.csv"
SLEEPER_URL = "https://api.sleeper.app/v1/players/nba"

USE_SLEEPER = True
USE_SALARIOS = True

COLS = ["date", "tm", "loc", "opp", "res", "tmpts", "opppts", "mp",
        "fg", "fga", "fg3", "fg3a", "ft", "fta",
        "orb", "drb", "trb", "ast", "stl", "blk", "tov", "pf",
        "pts", "pm", "reason"]

MAP = {
    "MIN": "mp", "FGM": "fg", "FGA": "fga", "FG3M": "fg3", "FG3A": "fg3a",
    "FTM": "ft", "FTA": "fta", "OREB": "orb", "DREB": "drb", "REB": "trb",
    "AST": "ast", "STL": "stl", "BLK": "blk", "TOV": "tov", "PF": "pf",
    "PTS": "pts", "PLUS_MINUS": "pm",
}

SCORING = {"pts": 1, "trb": 1.2, "ast": 1.5, "stl": 3, "blk": 3,
           "tov": -1, "tech": -0.5}


# ----------------------------------------------------------------------------
def temporada_de(hoje=None):
    """Ano final da temporada. Em jul/2026 a corrente ainda e 2025-26 = 2026."""
    hoje = hoje or datetime.now()
    return hoje.year + 1 if hoje.month >= 9 else hoje.year


def chave_nome(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def num(v):
    if v in (None, "", "None", "nan", "NaN"):
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 2)
    except (TypeError, ValueError):
        return None


def data_iso(v):
    """20251022 -> 2025-10-22"""
    s = str(v or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return ""


def idade_decimal(nasc):
    if not nasc:
        return None
    try:
        d = datetime.strptime(str(nasc)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return round((datetime.now() - d).days / 365.25, 1)


def destino(nome):
    if os.path.isabs(nome):
        return nome
    try:
        pasta = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pasta = os.getcwd()
    return os.path.join(pasta or os.getcwd(), nome)


# ----------------------------------------------------------------------------
def baixar_jogos(ano):
    url = GAMES_URL.format(ano=ano)
    print(f"[1/4] Baixando os jogos de {ano-1}-{str(ano)[-2:]}...")
    print(f"      {url}")
    r = requests.get(url, timeout=TIMEOUT)
    if r.status_code == 404:
        sys.exit(f"A temporada {ano} ainda nao existe no dataset. "
                 f"Ajuste SEASON no topo do arquivo.")
    r.raise_for_status()
    print(f"      {len(r.content)/1e6:.0f} MB baixados")
    return r.text


def montar(texto):
    print("[2/4] Lendo o CSV...")
    hoje = datetime.now().strftime("%Y-%m-%d")

    linhas, pontos, times_do_jogo = [], {}, {}
    for x in csv.DictReader(io.StringIO(texto)):
        dt = data_iso(x.get("date"))
        if not dt or dt >= hoje:          # so jogos encerrados, de ontem para tras
            continue
        gid = str(x.get("GAME_ID") or "").strip()
        tm = (x.get("TEAM_ABBREVIATION") or "").strip()
        pid = str(x.get("PLAYER_ID") or "").strip()
        if not (gid and tm and pid):
            continue

        rec = {c: None for c in COLS}
        rec["date"] = dt
        rec["tm"] = tm
        for col, chave in MAP.items():
            rec[chave] = num(x.get(col))

        linhas.append({"pid": pid, "gid": gid, "tm": tm,
                       "nome": (x.get("PLAYER_NAME") or "").strip(),
                       "idade": num(x.get("AGE")), "rec": rec})
        pontos[(gid, tm)] = pontos.get((gid, tm), 0) + (rec["pts"] or 0)
        times_do_jogo.setdefault(gid, set()).add(tm)

    print(f"      {len(linhas)} linhas | {len(times_do_jogo)} jogos")
    if not linhas:
        sys.exit("Nenhum jogo encerrado encontrado.")

    print("[3/4] Placar, adversario e jogos sem minuto...")
    for L in linhas:
        outros = [t for t in times_do_jogo.get(L["gid"], ()) if t != L["tm"]]
        adv = outros[0] if outros else ""
        tp, op = pontos.get((L["gid"], L["tm"])), pontos.get((L["gid"], adv))
        L["rec"]["opp"] = adv
        L["rec"]["tmpts"] = tp
        L["rec"]["opppts"] = op
        if tp is not None and op is not None:
            L["rec"]["res"] = "W" if tp > op else ("L" if tp < op else "")

    cal = {}
    for gid, times in times_do_jogo.items():
        for t in times:
            cal.setdefault(t, {}).setdefault(gid, None)
    for L in linhas:
        cal[L["tm"]][L["gid"]] = L["rec"]["date"]

    jogadores = {}
    for L in linhas:
        p = jogadores.get(L["pid"])
        if not p:
            p = {"id": L["pid"], "name": L["nome"], "team": L["tm"], "pos": "",
                 "age": L["idade"], "bio": {}, "_rows": {}}
            jogadores[L["pid"]] = p
        p["team"] = L["tm"]
        if L["idade"] and not p["age"]:
            p["age"] = L["idade"]
        p["_rows"][L["gid"]] = (L["rec"], L["tm"])

    dnp = 0
    for p in jogadores.values():
        por_time = {}
        for gid, (rec, tm) in p["_rows"].items():
            por_time.setdefault(tm, []).append(rec["date"])
        faltando = []
        for tm, datas in por_time.items():
            ini, fim = min(datas), max(datas)
            for gid, dt in (cal.get(tm) or {}).items():
                if gid in p["_rows"] or not dt:
                    continue
                if ini <= dt <= fim:
                    rec = {c: None for c in COLS}
                    rec["date"] = dt
                    rec["tm"] = tm
                    rec["reason"] = "Nao jogou"
                    faltando.append(rec)
        dnp += len(faltando)
        jogou = [r for r, _ in p["_rows"].values()]
        jogou.sort(key=lambda r: r["date"])

        # A janela conta jogos DISPUTADOS. Se contasse linhas, um jogador que
        # ficou muito tempo lesionado teria a janela tomada por DNPs e sobraria
        # quase nenhum jogo real para as medias.
        if len(jogou) > JOGOS_POR_TIME:
            jogou = jogou[-JOGOS_POR_TIME:]
        corte = jogou[0]["date"] if jogou else None

        todos = jogou + [r for r in faltando if corte and r["date"] >= corte]
        todos.sort(key=lambda r: r["date"])
        p["g"] = [[r[c] for c in COLS] for r in todos]
        del p["_rows"]

    print(f"      {len(jogadores)} jogadores | {dnp} jogos sem minuto reconstruidos")
    return jogadores


def enriquecer(jogadores):
    print("[4/4] Bios e contratos...")
    if USE_SLEEPER:
        try:
            bruto = requests.get(SLEEPER_URL, timeout=120).json()
            por_nome = {}
            for x in bruto.values():
                if not isinstance(x, dict):
                    continue
                nome = x.get("full_name") or f"{x.get('first_name','')} {x.get('last_name','')}"
                k = chave_nome(nome)
                if k:
                    por_nome[k] = x
            n = 0
            for p in jogadores.values():
                sl = por_nome.get(chave_nome(p["name"]))
                if not sl:
                    continue
                if sl.get("birth_date"):
                    p["bio"]["born"] = sl["birth_date"]
                    p["age"] = idade_decimal(sl["birth_date"]) or p["age"]
                if sl.get("years_exp") is not None:
                    p["bio"]["exp"] = num(sl["years_exp"])
                if sl.get("height"):
                    p["bio"]["height"] = sl["height"]
                if sl.get("weight"):
                    p["bio"]["weight"] = num(sl["weight"])
                if sl.get("college"):
                    p["bio"]["college"] = sl["college"]
                if sl.get("position") and not p["pos"]:
                    p["pos"] = sl["position"]
                n += 1
            print(f"      Sleeper: {n} jogadores")
        except Exception as e:  # noqa: BLE001
            print(f"      Sleeper pulado: {str(e)[:70]}")

    if USE_SALARIOS:
        try:
            txt = requests.get(SALARIO_URL, timeout=60).text
            linhas = list(csv.DictReader(io.StringIO(txt)))
            cols = sorted(c for c in (linhas[0].keys() if linhas else [])
                          if re.fullmatch(r"\d{4}-\d{2}", c or ""))
            if cols:
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
                print(f"      Contratos: {n} jogadores (temporada {atual})")
        except Exception as e:  # noqa: BLE001
            print(f"      Contratos pulados: {str(e)[:70]}")


# ----------------------------------------------------------------------------
def main():
    ano = SEASON or temporada_de()
    jogadores = montar(baixar_jogos(ano))
    enriquecer(jogadores)

    lista = sorted(jogadores.values(), key=lambda p: p["name"])
    i_data = COLS.index("date")
    ate = max((r[i_data] for p in lista for r in p["g"] if r[i_data]), default="")

    payload = {
        "cols": COLS,
        "scoring": SCORING,
        "meta": {
            "fonte": "github/gabriel1200/player_sheets",
            "season": f"{ano-1}-{str(ano)[-2:]}",
            "jogos_por_jogador": JOGOS_POR_TIME,
            "ate": ate,
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
        },
        "players": lista,
    }

    caminho = destino(OUT)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\n{caminho}")
    print(f"{os.path.getsize(caminho)/1e6:.1f} MB | {len(lista)} jogadores | jogos ate {ate}")


if __name__ == "__main__":
    main()
