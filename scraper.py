"""
Парсер публичных страниц лиги «Шесть на шесть» для команды «Имена».
Сохраняет последний удачный снимок в data.json, чтобы сайт работал и при
временной недоступности источника.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(__import__("os").environ.get("IMENA_DATA_DIR", str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"
TEAM_URL = "https://msk-6x6.ru/tournament/1065992/teams/application?team_id=1543529"
CALENDAR_URL = "https://msk-6x6.ru/tournament/1065992/calendar"
TOURNAMENT_URL = "https://msk-6x6.ru/tournament/1065992"
TABLE_URL = "https://msk-6x6.ru/tournament/1065992/tables"
LEAGUE_STATS_URL = "https://msk-6x6.ru/tournament/1065992/stats"
BASE_URL = "https://msk-6x6.ru"
TEAM_NAME = "Имена"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ImenaFootballAnalytics/1.0; +https://msk-6x6.ru/)",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
})

def get_soup(url: str) -> BeautifulSoup:
    response = SESSION.get(url, timeout=25)
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", "text/html"):
        raise RuntimeError(f"Источник вернул не HTML: {url}")
    return BeautifulSoup(response.text, "html.parser")

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def int_from(text: str, default: int = 0) -> int:
    match = re.search(r"-?\d+", text or "")
    return int(match.group()) if match else default

def parse_players(soup: BeautifulSoup) -> list[dict]:
    found = []
    seen = set()
    for row in soup.select("tr"):
        link = row.select_one('a[href*="/player/"]')
        if not link:
            continue
        name = clean(link.get_text(" ", strip=True))
        href = link.get("href", "")
        id_match = re.search(r"/player/(\d+)", href)
        if not name or name in seen:
            continue
        cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all(["td", "th"])]
        # On the league roster, numeric stats appear at the end of each player row:
        # games, goals, assists, yellow cards, red cards.
        numeric = []
        for cell in cells:
            if re.fullmatch(r"\d+(?:\s*\(\d+\))?", cell):
                numeric.append(int_from(cell))
        # Avoid counting a shirt number/age if present; use the five trailing stat cells.
        stats = numeric[-5:] if len(numeric) >= 5 else numeric
        stats = ([0] * (5-len(stats)) + stats)[-5:]
        position = ""
        for cell in cells:
            if cell in ("Вр.", "Зщ.", "Пз.", "Нп.", "Ун.", "Вр", "Зщ", "Пз", "Нп", "Ун"):
                position = {"Вр.":"Вратарь","Зщ.":"Защитник","Пз.":"Полузащитник",
                            "Нп.":"Нападающий","Ун.":"Универсал"}.get(cell, cell)
                break
        player = {
            "name": name,
            "games": stats[0],
            "goals": stats[1],
            "assists": stats[2],
            "yellow": stats[3],
            "red": stats[4],
            "position": position or "Игрок команды",
            "id": id_match.group(1) if id_match else None,
            "profile_url": urljoin(BASE_URL, href),
        }
        found.append(player)
        seen.add(name)
    if not found:
        raise RuntimeError("Не удалось найти список игроков на странице заявки. Возможно, сайт изменил разметку.")
    return found

def parse_profile_match_log(soup: BeautifulSoup) -> list[dict]:
    """Parse match-by-match stats from a league player profile when available."""
    month_map = {
        "янв": "01", "фев": "02", "мар": "03", "апр": "04", "май": "05", "июн": "06",
        "июл": "07", "авг": "08", "сен": "09", "окт": "10", "ноя": "11", "дек": "12",
    }
    current_year = datetime.now().year
    parsed = []
    seen = set()
    for row in soup.select("tr"):
        row_text = clean(row.get_text(" ", strip=True))
        if not row_text or not re.search(r"\d{1,2}\s+(?:янв|фев|мар|апр|май|июн|июл|авг|сен|сент|окт|ноя|дек)[а-яё]*\.?", row_text, flags=re.I):
            continue
        # Only keep rows that look like a completed match from the tournament.
        score_match = re.search(r"(\d{1,2})\s*[-:]\s*(\d{1,2})", row_text)
        if not score_match:
            continue
        date_match = re.search(r"(\d{1,2})\s+(янв[а-яё]*|фев[а-яё]*|мар[а-яё]*|апр[а-яё]*|май[а-яё]*|июн[а-яё]*|июл[а-яё]*|авг[а-яё]*|сен[а-яё]*|сент[а-яё]*|окт[а-яё]*|ноя[а-яё]*|дек[а-яё]*)\.?", row_text, flags=re.I)
        if not date_match:
            continue
        month_key = date_match.group(2).lower()[:3]
        month = month_map.get(month_key)
        if not month:
            continue
        day = int(date_match.group(1))
        year = current_year
        # A season can cross the year boundary; infer a year rollover conservatively.
        if month == "12" and datetime.now().month <= 2:
            year -= 1
        elif month in ("01", "02") and datetime.now().month >= 11:
            year += 1
        try:
            date_iso = f"{year}-{month}-{day:02d}"
            datetime.strptime(date_iso, "%Y-%m-%d")
        except ValueError:
            continue
        team_links = []
        for a in row.find_all("a"):
            label = clean(a.get_text(" ", strip=True))
            href = a.get("href", "")
            if not label or re.fullmatch(r"\d+\s*[-:]\s*\d+", label):
                continue
            if any(part in href for part in ("/tournament/", "/tour/", "/player/")):
                continue
            if len(label) < 60:
                team_links.append(label)
        team_links = list(dict.fromkeys(team_links))
        opponent = next((x for x in team_links if x.casefold() != TEAM_NAME.casefold()), "")
        # Per-match profile table stores goals, penalty-goal info, assists and cards.
        numeric_cells = []
        for cell in row.find_all(["td", "th"]):
            value = clean(cell.get_text(" ", strip=True))
            if re.fullmatch(r"\d+(?:\s*\(\d+\))?", value):
                numeric_cells.append(int_from(value))
        # The last five numeric cells are the per-match stat columns when present:
        # games, goals, assists, yellow, red. Some page versions omit games.
        if len(numeric_cells) >= 5:
            stats = numeric_cells[-5:]
            goals, assists, yellow, red = stats[1], stats[2], stats[3], stats[4]
        elif len(numeric_cells) >= 4:
            stats = numeric_cells[-4:]
            goals, assists, yellow, red = stats[0], stats[1], stats[2], stats[3]
        else:
            continue
        key = (date_iso, opponent, goals, assists, yellow, red)
        if key in seen:
            continue
        seen.add(key)
        parsed.append({"date": date_iso, "opponent": opponent, "goals": goals,
                       "assists": assists, "yellow": yellow, "red": red,
                       "source": "league_player_profile"})
    parsed.sort(key=lambda item: item["date"])
    return parsed

def _image_url_from_value(value: str, page_url: str) -> str:
    """Normalize an image URL found in src, srcset, CSS, or an anchor."""
    value = clean(value).strip("'\"")
    if not value:
        return ""
    if "," in value and (" 1x" in value or " 2x" in value or re.search(r"\s\d+w", value)):
        pieces = [piece.strip().split()[0] for piece in value.split(",") if piece.strip()]
        if pieces:
            value = pieces[-1]
    value = value.split()[0] if value else ""
    if value.startswith("//"):
        value = "https:" + value
    return urljoin(page_url, value) if value else ""


def _is_real_player_photo(url: str, player_id: str = "") -> bool:
    if not url:
        return False
    lower = url.lower()
    if any(bad in lower for bad in (
        "mc.yandex", "favicon", "logo", "banner", "placeholder",
        "player_football_photo_640x640.png", "default-avatar", "no-photo"
    )):
        return False
    is_image = bool(re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", lower))
    is_player_host = "st.joinsport.io/player/" in lower and "/photo/" in lower
    if player_id and is_player_host and f"/player/{player_id}/photo/" not in lower:
        return False
    return is_player_host or is_image


def extract_profile_photo(soup: BeautifulSoup, page_url: str, player_id: str = "") -> str:
    """Extract portraits despite the league's different image/lazy-load markup."""
    candidates: list[tuple[int, str]] = []

    def add(value, score=0):
        url = _image_url_from_value(str(value or ""), page_url)
        if _is_real_player_photo(url, player_id):
            candidates.append((score, url))

    for img in soup.find_all("img"):
        alt = clean(img.get("alt", "")).casefold()
        title = clean(img.get("title", "")).casefold()
        explicit = "фото игрока" in alt or "фото игрока" in title
        base_score = 100 if explicit else 10
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-image",
                     "data-fallback-src", "data-srcset", "srcset"):
            if img.get(attr):
                raw = str(img.get(attr))
                add(raw, base_score + (20 if "joinsport.io/player/" in raw.lower() else 0))
        anchor = img.find_parent("a", href=True)
        if anchor:
            href = anchor.get("href", "")
            if "joinsport.io/player/" in href.lower() or re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", href, re.I):
                add(href, base_score + 35)

    # Explicit image metadata sometimes contains the portrait when src is lazy-loaded.
    for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]', 'meta[property="twitter:image"]'):
        for tag in soup.select(selector):
            add(tag.get("content", ""), 60)

    # The league profile often wraps the portrait in an anchor to the original image.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "joinsport.io/player/" in href.lower() or re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", href, re.I):
            label = clean(a.get_text(" ", strip=True)).casefold()
            add(href, 90 if "фото игрока" in label else 55)
    for source in soup.find_all("source"):
        add(source.get("srcset", "") or source.get("src", ""), 20)
    for tag in soup.find_all(style=True):
        for raw_url in re.findall(r"background-image\s*:\s*url\((.*?)\)", tag.get("style", ""), flags=re.I):
            add(raw_url, 15)

    if not candidates:
        return ""
    best: dict[str, int] = {}
    for score, url in candidates:
        best[url] = max(score, best.get(url, -1))
    return max(best.items(), key=lambda item: item[1])[0]


def enrich_player_photo(player: dict, previous_by_id: dict | None = None,
                        previous_by_name: dict | None = None) -> dict:
    """Get portrait and per-match stats from a public league player profile."""
    url = player.get("profile_url")
    previous_by_id = previous_by_id or {}
    previous_by_name = previous_by_name or {}
    previous = previous_by_id.get(str(player.get("id") or "")) or previous_by_name.get(player.get("name", "")) or {}
    if not url:
        player["photo"] = previous.get("photo", "")
        player.setdefault("match_log", [])
        player["photo_source"] = "cached" if player["photo"] else "missing"
        return player
    try:
        soup = get_soup(url)
        player["match_log"] = parse_profile_match_log(soup)
        photo = extract_profile_photo(soup, url, str(player.get("id") or ""))
        if photo:
            player["photo"] = photo
            player["photo_source"] = "league_profile"
            player.pop("photo_error", None)
        else:
            player["photo"] = previous.get("photo", "")
            player["photo_source"] = "cached" if player["photo"] else "missing"
        text = clean(soup.get_text(" ", strip=True))
        patterns = {
            "games": r"Игры\s+(\d+)", "goals": r"Голы\s+(\d+)",
            "assists": r"Передачи\s+(\d+)", "yellow": r"ЖК\s+(\d+)", "red": r"КК\s+(\d+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.I)
            if match:
                player[key] = int(match.group(1))
    except Exception as exc:
        player["photo"] = previous.get("photo", "")
        player["photo_source"] = "cached" if player["photo"] else "missing"
        player.setdefault("match_log", [])
        player["photo_error"] = str(exc)[:160]
    return player

def date_from_context(text: str) -> str:
    months = {
        "января":"01","февраля":"02","марта":"03","апреля":"04","мая":"05","июня":"06",
        "июля":"07","августа":"08","сентября":"09","октября":"10","ноября":"11","декабря":"12",
    }
    m = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)", text.lower())
    if not m:
        return ""
    year = datetime.now().year
    return f"{year}-{months[m.group(2)]}-{int(m.group(1)):02d}"

def parse_matches(soup: BeautifulSoup) -> list[dict]:
    result = []
    seen = set()
    for score_link in soup.find_all("a"):
        score_text = clean(score_link.get_text(" ", strip=True))
        score_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)(?:\s*ТП)?", score_text, flags=re.I)
        if not score_match:
            continue
        # Find the nearest event container containing this score and two team links.
        event = None
        for parent in score_link.parents:
            if getattr(parent, "name", None) in ("body", "html"):
                break
            links = parent.find_all("a")
            if score_link in links and len(links) >= 3:
                # Smallest parent with enough links is usually one match card.
                valid_team_links = []
                for a in links:
                    t = clean(a.get_text(" ", strip=True))
                    h = a.get("href", "")
                    if a is score_link or re.fullmatch(r"\d+\s*-\s*\d+(?:\s*ТП)?", t, flags=re.I):
                        continue
                    if not t or (any(x in h for x in ("/tour/", "/player/", "/stadion")) or ("/tournament/" in h and "team_id=" not in h)):
                        continue
                    if re.search(r"\d{1,2}:\d{2}", t):
                        continue
                    valid_team_links.append(a)
                if len(valid_team_links) >= 2:
                    event = parent
                    break
        if not event:
            continue
        links = event.find_all("a")
        score_index = next((i for i,a in enumerate(links) if a is score_link), -1)
        if score_index < 0:
            continue
        def valid_team(a):
            t = clean(a.get_text(" ", strip=True))
            h = a.get("href", "")
            return bool(t and a is not score_link and not re.fullmatch(r"\d+\s*-\s*\d+(?:\s*ТП)?", t, flags=re.I)
                        and not (any(x in h for x in ("/tour/", "/player/", "/stadion")) or ("/tournament/" in h and "team_id=" not in h))
                        and not re.search(r"\d{1,2}:\d{2}", t)
                        and len(t) < 60)
        before = [a for a in links[:score_index] if valid_team(a)]
        after = [a for a in links[score_index+1:] if valid_team(a)]
        if not before or not after:
            # Some markup wraps the score in between both team anchors but DOM order can differ.
            teams = [a for a in links if valid_team(a)]
            if len(teams) < 2:
                continue
            left, right = teams[-2], teams[-1]
        else:
            left, right = before[-1], after[0]
        home, away = clean(left.get_text(" ", strip=True)), clean(right.get_text(" ", strip=True))
        if home == away or len(home) > 60 or len(away) > 60:
            continue
        g1, g2 = int(score_match.group(1)), int(score_match.group(2))
        if TEAM_NAME not in (home, away):
            continue
        if home == TEAM_NAME:
            gf, ga, opponent = g1, g2, away
        else:
            gf, ga, opponent = g2, g1, home
        context = clean(event.get_text(" ", strip=True))
        date = date_from_context(context)
        key = (date, opponent, gf, ga)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "date": date or "",
            "date_label": date or "Дата не указана",
            "opponent": opponent,
            "for": gf,
            "against": ga,
            "venue": "7 сезон",
            "source_url": CALENDAR_URL,
        })
    # Sort chronologically; empty dates go last.
    result.sort(key=lambda m: (m["date"] == "", m["date"]))
    if not result:
        raise RuntimeError("Не удалось разобрать результаты матчей. Проверьте, доступна ли страница календаря.")
    return result

def parse_all_league_results(soup: BeautifulSoup) -> list[dict]:
    """Extract all completed team-vs-team results from the public tournament calendar."""
    results = []
    seen = set()
    for score_link in soup.find_all("a"):
        score_text = clean(score_link.get_text(" ", strip=True))
        score_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)(?:\s*ТП)?", score_text, flags=re.I)
        if not score_match:
            continue
        event = None
        for parent in score_link.parents:
            if getattr(parent, "name", None) in ("body", "html"):
                break
            links = parent.find_all("a")
            if score_link in links and len(links) >= 3:
                candidates = []
                for a in links:
                    label = clean(a.get_text(" ", strip=True))
                    if a is score_link or not label or re.fullmatch(r"\d+\s*-\s*\d+(?:\s*ТП)?", label, re.I):
                        continue
                    if "/player/" in a.get("href", "") or "/tour/" in a.get("href", ""):
                        continue
                    if re.search(r"\d{1,2}:\d{2}", label) or re.search(r"\bтур\b|чемпионат|стадион|все результаты", label, re.I):
                        continue
                    if len(label) < 60:
                        candidates.append(a)
                if len(candidates) >= 2:
                    event = parent
                    break
        if not event:
            continue
        links = event.find_all("a")
        score_index = next((i for i, a in enumerate(links) if a is score_link), -1)
        def valid_team(a):
            label = clean(a.get_text(" ", strip=True))
            href = a.get("href", "")
            return bool(label and a is not score_link and "/player/" not in href and "/tour/" not in href
                        and not re.fullmatch(r"\d+\s*-\s*\d+(?:\s*ТП)?", label, re.I)
                        and not re.search(r"\d{1,2}:\d{2}", label)
                        and not re.search(r"\bтур\b|чемпионат|стадион|все результаты", label, re.I)
                        and len(label) < 60)
        before = [a for a in links[:score_index] if valid_team(a)]
        after = [a for a in links[score_index+1:] if valid_team(a)]
        if not before or not after:
            teams = [a for a in links if valid_team(a)]
            if len(teams) < 2:
                continue
            home_link, away_link = teams[-2], teams[-1]
        else:
            home_link, away_link = before[-1], after[0]
        home = clean(home_link.get_text(" ", strip=True))
        away = clean(away_link.get_text(" ", strip=True))
        if home == away:
            continue
        goals_home, goals_away = int(score_match.group(1)), int(score_match.group(2))
        context = clean(event.get_text(" ", strip=True))
        date = date_from_context(context)
        key = (date, home.casefold(), away.casefold(), goals_home, goals_away)
        if key in seen:
            continue
        seen.add(key)
        results.append({"date": date, "home": home, "away": away,
                        "home_goals": goals_home, "away_goals": goals_away})
    return results


def standings_from_results(official_table: list[dict], results: list[dict]) -> list[dict]:
    """Fill W/D/L and goal difference only when parsed results match official games/points."""
    stats = {}
    for row in official_table:
        stats[row["team"].casefold()] = {"played": 0, "wins": 0, "draws": 0, "losses": 0,
                                         "gf": 0, "ga": 0, "points": 0}
    for match in results:
        home, away = match["home"].casefold(), match["away"].casefold()
        if home not in stats or away not in stats:
            continue
        hg, ag = match["home_goals"], match["away_goals"]
        for key, gf, ga in ((home, hg, ag), (away, ag, hg)):
            stats[key]["played"] += 1
            stats[key]["gf"] += gf
            stats[key]["ga"] += ga
        if hg > ag:
            stats[home]["wins"] += 1; stats[home]["points"] += 3; stats[away]["losses"] += 1
        elif ag > hg:
            stats[away]["wins"] += 1; stats[away]["points"] += 3; stats[home]["losses"] += 1
        else:
            stats[home]["draws"] += 1; stats[away]["draws"] += 1
            stats[home]["points"] += 1; stats[away]["points"] += 1
    enriched = []
    for row in official_table:
        item = dict(row)
        detail = stats.get(row["team"].casefold(), {})
        # Do not replace official data with partial or stale match logs.
        if detail and detail["played"] == row.get("played") and detail["points"] == row.get("points"):
            item.update(detail)
            item["gd"] = item["gf"] - item["ga"]
        enriched.append(item)
    return enriched

def parse_league_table(soup: BeautifulSoup) -> list[dict]:
    """Parse either the compact current table or the detailed table page."""
    rows = []
    seen = set()
    for row in soup.select("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        team_link = next((a for a in row.find_all("a", href=True)
                          if clean(a.get_text(" ", strip=True)) and "/player/" not in a.get("href", "")), None)
        if not team_link:
            continue
        name = clean(team_link.get_text(" ", strip=True))
        if not name or name.casefold() in seen or name.casefold() in {"команда", "вся таблица"}:
            continue
        nums = []
        goal_diff = None
        for c in cells:
            if re.fullmatch(r"\d+", c):
                nums.append(int(c))
            elif re.fullmatch(r"\d+\s*[-–:]\s*\d+", c):
                pair = re.split(r"\s*[-–:]\s*", c)
                goal_diff = (int(pair[0]), int(pair[1]))
        if goal_diff is not None and len(nums) >= 6:
            position, games, wins, draws, losses = nums[:5]
            points = nums[5]
            gf, ga = goal_diff
        elif len(nums) >= 3:
            # Current tournament landing page has position, games and points only.
            position, games, points = nums[:3]
            wins = draws = losses = None
            gf = ga = None
        else:
            continue
        rows.append({"position": position, "team": name, "played": games, "wins": wins,
                     "draws": draws, "losses": losses, "gf": gf, "ga": ga,
                     "gd": gf-ga if gf is not None and ga is not None else None, "points": points})
        seen.add(name.casefold())
    rows.sort(key=lambda x: x["position"])
    if not rows:
        raise RuntimeError("Не удалось разобрать таблицу лиги")
    return rows


def parse_league_scorers(soup: BeautifulSoup) -> list[dict]:
    """Parse the league-wide goalscorer ranking from the public stats page."""
    scorers = []
    seen = set()
    for row in soup.select("tr"):
        player_link = row.select_one('a[href*="/player/"]')
        if not player_link:
            continue
        player_name = clean(player_link.get_text(" ", strip=True))
        team_link = next((a for a in row.find_all("a", href=True)
                          if a is not player_link and "/player/" not in a.get("href", "")
                          and clean(a.get_text(" ", strip=True))), None)
        if not player_name or not team_link:
            continue
        team_name = clean(team_link.get_text(" ", strip=True))
        cells = [clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        numeric = [int_from(c) for c in cells if re.fullmatch(r"\d+(?:\s*\(\d+\))?", c)]
        if len(numeric) < 2:
            continue
        games, goals = numeric[-2], numeric[-1]
        key = (player_name.casefold(), team_name.casefold())
        if key in seen:
            continue
        scorers.append({"player": player_name, "team": team_name, "games": games, "goals": goals,
                        "goals_per_game": round(goals/games, 2) if games else 0})
        seen.add(key)
    scorers.sort(key=lambda x: (-x["goals"], -x["goals_per_game"], x["player"].casefold()))
    if not scorers:
        raise RuntimeError("Не удалось разобрать бомбардиров лиги")
    for i, item in enumerate(scorers, 1):
        item["rank"] = i
    return scorers

def load_cache() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "team": TEAM_NAME, "players": [], "matches": [], "league_table": [], "league_scorers": [],
        "updated_at": None, "source_status": "empty",
        "last_error": "Данные ещё не загружались",
        "source_urls": {"team": TEAM_URL, "calendar": CALENDAR_URL, "tournament": TOURNAMENT_URL, "table": TABLE_URL, "stats": LEAGUE_STATS_URL},
    }

def save_cache(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def refresh_data() -> dict:
    previous = load_cache()
    errors = []
    try:
        team_soup = get_soup(TEAM_URL)
        players = parse_players(team_soup)
        # Reuse confirmed photos if a profile temporarily fails; recheck every
        # roster profile so newly added portraits on the league site are discovered.
        old_players = previous.get("players", [])
        previous_by_id = {str(p.get("id")): p for p in old_players if p.get("id")}
        previous_by_name = {p.get("name", ""): p for p in old_players if p.get("name")}
        photo_counts = {"found": 0, "cached": 0, "missing": 0}
        photo_missing_names = []
        photo_found_names = []
        for player in players:
            enrich_player_photo(player, previous_by_id, previous_by_name)
            source = player.get("photo_source", "missing")
            if player.get("photo"):
                photo_counts["found" if source == "league_profile" else "cached"] += 1
                photo_found_names.append(player.get("name", "Без имени"))
            else:
                photo_counts["missing"] += 1
                photo_missing_names.append(player.get("name", "Без имени"))
            time.sleep(0.15)
        calendar_soup = get_soup(CALENDAR_URL)
        matches = parse_matches(calendar_soup)
        all_league_results = parse_all_league_results(calendar_soup)
        league_table = previous.get("league_table", [])
        league_scorers = previous.get("league_scorers", [])
        league_errors = []
        try:
            current_table = parse_league_table(get_soup(TOURNAMENT_URL))
            try:
                detailed_table = parse_league_table(get_soup(TABLE_URL))
                details_by_name = {x["team"].casefold(): x for x in detailed_table}
                for row in current_table:
                    detail = details_by_name.get(row["team"].casefold())
                    if detail and detail.get("played") == row.get("played"):
                        for field in ("wins", "draws", "losses", "gf", "ga", "gd"):
                            if detail.get(field) is not None:
                                row[field] = detail[field]
            except Exception as exc:
                league_errors.append("детальная таблица: " + str(exc))
            league_table = standings_from_results(current_table, all_league_results)
        except Exception as exc:
            league_errors.append("таблица: " + str(exc))
        try:
            league_scorers = parse_league_scorers(get_soup(LEAGUE_STATS_URL))
        except Exception as exc:
            league_errors.append("бомбардиры: " + str(exc))
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "team": TEAM_NAME,
            "players": players,
            "matches": matches,
            "league_table": league_table,
            "league_scorers": league_scorers,
            "updated_at": now,
            "source_status": "ok",
            "last_error": "",
            "photo_summary": photo_counts,
            "photo_found_names": photo_found_names,
            "photo_missing_names": photo_missing_names,
            "source_urls": {"team": TEAM_URL, "calendar": CALENDAR_URL, "table": TABLE_URL, "stats": LEAGUE_STATS_URL},
            "league_warning": "; ".join(league_errors),
        }
        save_cache(data)
        return data
    except Exception as exc:
        errors.append(str(exc))
        if previous.get("players") or previous.get("matches"):
            previous["source_status"] = "stale"
            previous["last_error"] = "Обновление не удалось: " + "; ".join(errors)
            previous["source_urls"] = {"team": TEAM_URL, "calendar": CALENDAR_URL, "table": TABLE_URL, "stats": LEAGUE_STATS_URL}
            save_cache(previous)
            return previous
        raise RuntimeError("Не удалось загрузить данные с сайта лиги: " + "; ".join(errors)) from exc

if __name__ == "__main__":
    data = refresh_data()
    print(f"Статус: {data.get('source_status')}; игроков: {len(data.get('players', []))}; матчей: {len(data.get('matches', []))}")
    print(data.get("last_error", ""))
