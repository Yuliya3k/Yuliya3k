"""Fetch, compute and render private-safe GitHub profile statistics (Python 3.11+)."""
from __future__ import annotations

import argparse
import calendar
from collections import Counter
from datetime import date, datetime, time as daytime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("stats")
LOGIN = "Yuliya3k"
TYPES = {
    "unreal": ("Unreal Engine", "Unreal Engine games and plugins"),
    "unity": ("Unity", "Unity games and tools"),
    "renpy": ("Ren'Py visual novels", "Ren'Py projects"),
    "web": ("Web apps and sites", "TypeScript and JavaScript"),
    "tools": ("Tools and pipelines", "Python, PowerShell and shell"),
    "other": ("Other", "Everything else"),
}
THEMES = {
    "light": dict(surface="#ffffff", border="#d0d7de", ink="#1f2328", secondary="#59636e", muted="#818b98", grid="#e6e9ec", baseline="#d0d7de", accent="#2a78d6", track="#eef1f4"),
    "dark": dict(surface="#0d1117", border="#3d444d", ink="#f0f6fc", secondary="#9198a1", muted="#6e7681", grid="#21262d", baseline="#3d444d", accent="#3987e5", track="#161b22"),
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"


def today_madrid():
    try:
        return datetime.now(ZoneInfo("Europe/Madrid")).date()
    except ZoneInfoNotFoundError:
        LOG.warning("Europe/Madrid tzdata unavailable; using UTC")
        return datetime.now(timezone.utc).date()


def graphql(token, query, variables=None):
    """Retry transport failures only; response bodies and queries are never logged."""
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": "bearer " + token, "Content-Type": "application/json", "User-Agent": "profile-static-stats"},
    )
    for attempt in range(1, 4):
        LOG.info("graphql entry attempt=%d timeout=60", attempt)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                LOG.info("graphql status=%s rate_limit=%s remaining=%s reset=%s", response.status, response.headers.get("X-RateLimit-Limit"), response.headers.get("X-RateLimit-Remaining"), response.headers.get("X-RateLimit-Reset"))
                payload = json.load(response)
            if payload.get("errors"):
                # API messages may contain repository identifiers or token fragments.
                # Report safe error categories rather than reflecting remote text.
                # Field and permission errors only: no query here passes a repository name.
                for error in payload["errors"]:
                    LOG.error("GraphQL error type=%s path=%s message=%s", error.get("type"), error.get("path"), error.get("message"))
                raise ValueError("GraphQL response contains errors")
            if not isinstance(payload.get("data"), dict):
                raise ValueError("GraphQL response has no data")
            LOG.info("graphql exit success")
            return payload["data"]
        except urllib.error.HTTPError as exc:
            LOG.warning("graphql HTTP status=%d remaining=%s reset=%s", exc.code, exc.headers.get("X-RateLimit-Remaining"), exc.headers.get("X-RateLimit-Reset"))
            if exc.code != 429 and not 500 <= exc.code <= 599:
                raise ValueError("GraphQL HTTP request rejected") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            LOG.warning("graphql network failure attempt=%d", attempt)
        if attempt < 3:
            LOG.info("graphql retry backoff_seconds=%d", 2 ** attempt)
            time.sleep(2 ** attempt)
    raise ValueError("GraphQL transport failed after 3 attempts")


def month_windows(created, today):
    start = datetime.fromisoformat(created.replace("Z", "+00:00"))
    finish = datetime.combine(today, daytime.max, timezone.utc)
    while start <= finish:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = min(next_month - timedelta(microseconds=1), finish)
        yield start.isoformat(), end.isoformat()
        start = next_month


def fetch(today):
    LOG.info("fetch entry")
    token = os.environ.get("METRICS_TOKEN")
    if not token:
        raise ValueError("METRICS_TOKEN is required without --fixture")
    viewer = graphql(token, "query { viewer { login createdAt } }")["viewer"]
    if viewer["login"].casefold() != LOGIN.casefold():
        raise ValueError("Token viewer does not match required account")
    raw = dict(login=viewer["login"], createdAt=viewer["createdAt"], commitsByDay={}, windows=[], repositories=[])
    query = """query($from: DateTime!, $to: DateTime!) { viewer {
      contributionsCollection(from: $from, to: $to) {
        totalCommitContributions restrictedContributionsCount
        commitContributionsByRepository(maxRepositories: 100) {
          repository { nameWithOwner isPrivate }
          contributions(first: 100) { totalCount pageInfo { hasNextPage }
            nodes { occurredAt commitCount } }
        }
      }
    } }"""
    for index, (start, end) in enumerate(month_windows(raw["createdAt"], today), 1):
        data = graphql(token, query, {"from": start, "to": end})["viewer"]["contributionsCollection"]
        repos = data["commitContributionsByRepository"]
        summed = 0
        if len(repos) == 100:
            LOG.warning("window#%d returned 100 repositories; possible truncation", index)
        for ri, repo in enumerate(repos, 1):
            contributions = repo["contributions"]
            if contributions["pageInfo"]["hasNextPage"]:
                LOG.warning("window#%d repo#%d private=%s contributions truncated", index, ri, repo["repository"]["isPrivate"])
            for node in contributions["nodes"]:
                day = node["occurredAt"][:10]
                raw["commitsByDay"][day] = raw["commitsByDay"].get(day, 0) + node["commitCount"]
                summed += node["commitCount"]
        LOG.info("window#%d from=%s to=%s total=%d sum=%d restricted=%d", index, start, end, data["totalCommitContributions"], summed, data["restrictedContributionsCount"])
        if summed != data["totalCommitContributions"]:
            LOG.warning("window#%d commit total mismatch", index)
        raw["windows"].append(dict(**{"from": start, "to": end}, total=data["totalCommitContributions"], sum=summed))
    query = """query($after: String) { viewer {
      repositories(first: 50, after: $after, ownerAffiliations: [OWNER, COLLABORATOR], isFork: false) {
        pageInfo { hasNextPage endCursor } totalCount nodes {
          nameWithOwner isPrivate isArchived createdAt primaryLanguage { name }
          languages(first: 30, orderBy: {field: SIZE, direction: DESC}) {
            totalSize edges { size node { name color } } }
          object(expression: "HEAD:") { ... on Tree { entries { name type } } }
        }
      }
    } }"""
    cursor = None
    seen = set()
    while True:
        data = graphql(token, query, {"after": cursor})["viewer"]["repositories"]
        LOG.info("repository page fetched count=%d total=%d", len(data["nodes"]), data["totalCount"])
        for repo in data["nodes"]:
            if repo["nameWithOwner"].split("/")[-1].casefold() == LOGIN.casefold():
                LOG.info("profile repository excluded")
                continue
            languages = [{"name": edge["node"]["name"], "size": edge["size"]} for edge in repo["languages"]["edges"]]
            if sum(lang["size"] for lang in languages) != repo["languages"]["totalSize"]:
                LOG.warning("repo#%d private=%s language byte total mismatch; possible language truncation", len(raw["repositories"]) + 1, repo["isPrivate"])
            raw["repositories"].append(dict(name=repo["nameWithOwner"], isPrivate=repo["isPrivate"], isArchived=repo["isArchived"], createdAt=repo["createdAt"], primaryLanguage=(repo["primaryLanguage"] or {}).get("name"), languages=languages, rootEntries=(repo["object"] or {}).get("entries", [])))
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
        if not cursor or cursor in seen:
            raise ValueError("Repository pagination did not advance")
        seen.add(cursor)
    LOG.info("fetch exit windows=%d repositories=%d private=%d archived=%d", len(raw["windows"]), len(raw["repositories"]), sum(r["isPrivate"] for r in raw["repositories"]), sum(r["isArchived"] for r in raw["repositories"]))
    return raw


def classify(repo):
    dirs = {e["name"].casefold() for e in repo["rootEntries"] if e["type"].casefold() == "tree"}
    files = {e["name"].casefold() for e in repo["rootEntries"] if e["type"].casefold() == "blob"}
    langs = Counter()
    for lang in repo["languages"]:
        langs[lang["name"].casefold()] += lang["size"]
    total = sum(langs.values())
    top = max(langs, key=langs.get) if total else ""
    if any(f.endswith((".uproject", ".uplugin")) for f in files):
        return "unreal", "root:engine-manifest"
    if {"source", "config"} <= dirs and langs["c++"]:
        return "unreal", "root:Source+Config"
    if {"assets", "projectsettings"} <= dirs:
        return "unity", "root:Assets+ProjectSettings"
    if langs["c#"] and (langs["shaderlab"] or langs["hlsl"]):
        return "unity", "languages:C#+shader"
    if "packages" in dirs and total and langs["c#"] / total >= .5:
        return "unity", "root:Packages+C#"
    if total and langs["ren'py"] / total >= .05:
        return "renpy", "language:Ren'Py>=5%"
    if "game" in dirs and langs["ren'py"]:
        return "renpy", "root:game+Ren'Py"
    if "package.json" in files:
        return "web", "root:package.json"
    if top in {"typescript", "javascript", "html", "css", "vue", "svelte", "astro"}:
        return "web", "language:web"
    if top in {"python", "powershell", "shell", "batchfile", "lua"}:
        return "tools", "language:tools"
    return "other", "fallback"


def compute(raw, today):
    LOG.info("compute entry windows=%d repositories=%d", len(raw["windows"]), len(raw["repositories"]))
    if raw["login"].casefold() != LOGIN.casefold():
        raise ValueError("Fixture viewer does not match required account")
    days = {}
    for day, value in raw["commitsByDay"].items():
        parsed = date.fromisoformat(day)
        if type(value) is not int or value < 0:
            raise ValueError("Commit counts must be nonnegative integers")
        if parsed <= today:
            days[parsed] = value
    repos = [r for r in raw["repositories"] if r["name"].split("/")[-1].casefold() != LOGIN.casefold() and not r.get("isFork", False)]
    languages, types = Counter(), Counter()
    for index, repo in enumerate(repos, 1):
        for lang in repo["languages"]:
            if type(lang["size"]) is not int or lang["size"] < 0:
                raise ValueError("Language sizes must be nonnegative integers")
            languages[lang["name"]] += lang["size"]
        kind, reason = classify(repo)
        types[kind] += 1
        LOG.info("repo#%d private=%s type=%s reason=%s", index, repo["isPrivate"], kind, reason)
    languages = Counter({k: v for k, v in languages.items() if v})
    total_bytes = sum(languages.values())
    ranked = sorted(languages.items(), key=lambda pair: (-pair[1], pair[0]))
    rows = ranked[:8]
    if len(ranked) > 8:
        rows.append((f"Other ({len(ranked) - 8} languages)", sum(n for _, n in ranked[8:])))
    LOG.info("languages bytes=%d count=%d top=%s", total_bytes, len(languages), json.dumps(rows))
    active = sorted(d for d, n in days.items() if n > 0)
    longest = run = 0
    previous = None
    for day in active:
        run = run + 1 if previous and day == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    end = today if days.get(today, 0) else today - timedelta(days=1)
    current = 0
    while days.get(end, 0):
        current += 1
        end -= timedelta(days=1)
    weeks = []
    for index in range(52):
        end = today - timedelta(days=(51 - index) * 7)
        start = end - timedelta(days=6)
        weeks.append(dict(start=start, end=end, value=sum(days.get(start + timedelta(days=i), 0) for i in range(7))))
    busiest = min(days, key=lambda d: (-days[d], d)) if days else today
    stats = dict(year=sum(n for d, n in days.items() if d >= today - timedelta(days=364)), all=sum(days.values()), month=sum(n for d, n in days.items() if d >= today - timedelta(days=29)), busiest=days.get(busiest, 0), busiest_date=busiest, streak=current, longest=longest, weeks=weeks, languages=rows, language_count=len(languages), bytes=total_bytes, types=types, repositories=len(repos), private=sum(r["isPrivate"] for r in repos), archived=sum(r["isArchived"] for r in repos))
    for key, value in stats.items():
        if isinstance(value, (int, date)):
            LOG.info("computed %s=%s", key, value)
    LOG.info("compute exit weeks=%d project_types=%s", len(weeks), dict(types))
    return stats


def number(value):
    if value < 10000:
        return f"{value:,}"
    return f"{value / (1000000 if value >= 1000000 else 1000):.1f}" + ("M" if value >= 1000000 else "K")


def width(text, size):
    """Conservative character advances, in em, for the system sans font stack."""
    advances = {**dict.fromkeys("ilI.,:;'!|", .30), **dict.fromkeys("mwMW@%", .94), **dict.fromkeys("0123456789", .62), " ": .32}
    return sum(advances.get(c, .66 if c.isupper() else .58) for c in str(text)) * size


class SVG:
    def __init__(self, theme, height, title, label):
        self.t = THEMES[theme]
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="840" height="{height}" viewBox="0 0 840 {height}" role="img" aria-label="{escape(label, {chr(34): "&quot;"})}"><title>{escape(title)}</title>', f'<g font-family="{escape(FONT, {chr(34): "&quot;"})}">', f'<rect x="0.5" y="0.5" width="839" height="{height-1}" rx="12" fill="{self.t["surface"]}" stroke="{self.t["border"]}"/>']

    def text(self, x, y, text, size=14, color="ink", weight=400, anchor="start", tabular=False):
        w = width(text, size)
        left = x - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
        if left < 18 or left + w > 822 or y - size < 0 or y > self.height - 12:
            raise ValueError("Text layout exceeds card bounds")
        style = ' style="font-variant-numeric:tabular-nums"' if tabular else ''
        self.parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" font-weight="{weight}" fill="{self.t[color]}" text-anchor="{anchor}"{style}>{escape(str(text))}</text>')

    def add(self, value):
        self.parts.append(value)

    def finish(self):
        return "".join(self.parts) + "</g></svg>\n"

    def header(self, title, subtitle):
        self.text(24, 43, title, 23, weight=600)
        self.text(24, 69, subtitle, 14, "muted")

    def bar(self, x, y, value, maximum, length, other=False):
        fill = self.t["muted" if other else "accent"]
        self.add(f'<rect x="{x}" y="{y}" width="{length}" height="16" rx="4" fill="{self.t["track"]}"/>')
        span = length * value / maximum if maximum else 0
        if span:
            radius = min(4, span / 2)
            self.add(f'<path d="M{x},{y} H{x+span-radius} Q{x+span},{y} {x+span},{y+radius} V{y+16-radius} Q{x+span},{y+16} {x+span-radius},{y+16} H{x} Z" fill="{fill}"/>')


def commits_card(stats, theme):
    s = SVG(theme, 616, "Commits", f'{number(stats["year"])} commits in the last 12 months; {number(stats["all"])} all time; current streak {stats["streak"]} days')
    s.header("Commits", "Default-branch commits, private repositories included")
    s.text(24, 146, number(stats["year"]), 60, weight=600)
    s.text(24, 176, "commits in the last 12 months", 15, "secondary")
    values = [("All time", number(stats["all"]), ""), ("Last 30 days", number(stats["month"]), ""), ("Busiest day", number(stats["busiest"]), f'{calendar.month_abbr[stats["busiest_date"].month]} {stats["busiest_date"].day}, {stats["busiest_date"].year}'), ("Current streak", f'{stats["streak"]} days', "")]
    for i, (label, value, detail) in enumerate(values):
        x, y = 410 + (i % 2) * 205, 108 + (i // 2) * 86
        s.text(x, y, label, 13, "secondary")
        s.text(x, y+31, value, 26, weight=600)
        if detail:
            s.text(x, y+51, detail, 12, "muted")
    s.text(24, 291, "Commits per week, last 12 months", 16, weight=600)
    weeks = stats["weeks"]
    peak = max(range(52), key=lambda i: weeks[i]["value"])
    highest = weeks[peak]["value"]
    magnitude = 10 ** math.floor(math.log10(max(highest / 4, 1)))
    step = next(n * magnitude for n in (1, 2, 2.5, 5, 10) if n * magnitude >= highest / 4)
    ceiling = max(step * 4, 4)
    x0, x1, top, bottom = 70, 768, 335, 555
    points = [(x0 + i*(x1-x0)/51, bottom - w["value"] / ceiling * (bottom-top)) for i,w in enumerate(weeks)]
    for i in range(5):
        y = bottom - i * (bottom-top)/4
        s.add(f'<path d="M{x0},{y} H{x1}" stroke="{s.t["baseline" if i == 0 else "grid"]}" fill="none"/>')
        s.text(x0-12, y+4, number(int(ceiling*i/4)), 12, "muted", anchor="end", tabular=True)
    path = "M" + " L".join(f"{x:.2f},{y:.2f}" for x,y in points)
    s.add(f'<path d="{path} L{x1},{bottom} L{x0},{bottom} Z" fill="{s.t["accent"]}" fill-opacity="0.1"/>')
    s.add(f'<path d="{path}" fill="none" stroke="{s.t["accent"]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    # Month labels use the first bin starting in each month; January includes year.
    # A month whose first bin sits within 34 px of the previous label is skipped.
    previous, last_x = None, -100.0
    for i,w in enumerate(weeks):
        month = (w["start"].year, w["start"].month)
        if month != previous and points[i][0] - last_x >= 34:
            s.text(points[i][0], 578, calendar.month_abbr[month[1]], 12, "muted", anchor="middle")
            if month[1] == 1:
                s.text(points[i][0], 595, str(month[0]), 12, "muted", anchor="middle")
            last_x = points[i][0]
        previous = month
    # Reserve a horizontal annotation band above the plot, connected to the peak.
    px, py = points[peak]
    if peak == 51:
        # The latest week is the peak: one label, no repeated end value.
        peak_label = f'This week: {number(highest)} commits, the highest of the year'
    else:
        peak_label = f'Peak: {number(highest)} commits, week of {calendar.month_abbr[weeks[peak]["start"].month]} {weeks[peak]["start"].day}'
    label_x = min(max(px, 24 + width(peak_label, 12)/2), 816-width(peak_label,12)/2)
    s.text(label_x, 319, peak_label, 12, "secondary", anchor="middle")
    s.add(f'<path d="M{px},{py-6} V326" fill="none" stroke="{s.t["baseline"]}"/>')
    for i in {peak, 51}:
        x,y = points[i]
        s.add(f'<circle cx="{x}" cy="{y}" r="4" fill="{s.t["accent"]}" stroke="{s.t["surface"]}" stroke-width="2"/>')
    if peak != 51:
        s.text(x1+10, points[-1][1]+4, number(weeks[-1]["value"]), 12, "secondary")
    return s.finish()


def languages_card(stats, theme):
    rows = stats["languages"]
    s = SVG(theme, max(170, 108+44*len(rows)), "Most used languages", f'{stats["language_count"]} languages across {stats["repositories"]} repositories, by code size')
    s.header("Most used languages", f'{stats["language_count"]} languages across {stats["repositories"]} repositories, by code size')
    label_width = max((width(name,18) for name,_ in rows), default=0)
    x = max(260, math.ceil(24+label_width+24))
    if x > 450:
        raise ValueError("Language label is too wide for readable bar layout")
    maximum = max((n for _, n in rows), default=0)
    for i,(name, value) in enumerate(rows):
        y = 108+i*44
        s.text(24, y+14, name, 18)
        s.bar(x, y, value, maximum, 730-x, name.startswith("Other ("))
        s.text(816, y+14, f'{value/stats["bytes"]*100:.1f}%', 18, weight=600, anchor="end")
    if not rows:
        s.text(24, 120, "No language bytes available", 18, "secondary")
    return s.finish()


def projects_card(stats, theme):
    rows = sorted(stats["types"].items(), key=lambda p: (p[0] == "other", -p[1], p[0]))
    s = SVG(theme, max(170, 100+68*len(rows)), "Project types", f'{stats["repositories"]} repositories, {stats["private"]} private')
    s.header("Project types", f'{stats["repositories"]} repositories, {stats["private"]} private')
    maximum = max((n for _,n in rows), default=0)
    for i,(kind,value) in enumerate(rows):
        y = 108+i*68
        label, description = TYPES[kind]
        if max(width(label,18),width(description,13)) > 320:
            raise ValueError("Project label exceeds allocated column")
        s.text(24,y+12,label,18)
        s.text(24,y+34,description,13,"muted")
        s.bar(370,y,value,maximum,390,kind == "other")
        s.text(816,y+14,number(value),18,weight=600,anchor="end")
    if not rows:
        s.text(24,120,"No repositories available",18,"secondary")
    return s.finish()


TOKEN_RE = re.compile(r"[A-Za-z0-9._'+#-]+")


def visible_tokens(content):
    """Casefolded word tokens a reader can see: text nodes, title and aria-label."""
    root = ET.fromstring(content)
    texts = list(root.itertext()) + [node.attrib.get("aria-label", "") for node in root.iter()]
    return {token.casefold().strip("._-'") for text in texts for token in TOKEN_RE.findall(text)} - {""}


def static_tokens():
    """Words every card shows by design, collected by rendering neutral stats.

    A repository whose name equals one of these words (for example a repository
    called "unity" or "commits") is not leaked by the card, so it must not fail
    the daily run. Language names come from GitHub's language list, not from
    repository data, and are exempt in privacy_check.
    """
    day = date(2000, 1, 1)
    weeks = [dict(start=day, end=day, value=0) for _ in range(52)]
    neutral = dict(year=0, all=0, month=0, busiest=0, busiest_date=day, streak=0, longest=0, weeks=weeks,
                   languages=[("Other (0 languages)", 1)], language_count=0, bytes=1,
                   types=Counter({kind: 1 for kind in TYPES}), repositories=0, private=0, archived=0)
    words = set()
    for theme in THEMES:
        for renderer in (commits_card, languages_card, projects_card):
            words |= visible_tokens(renderer(neutral, theme))
    words |= {m.casefold() for m in calendar.month_abbr if m}
    return words


def privacy_check(raw, stats, outputs):
    """Fail if any repository name is visible in a card.

    No renderer ever receives repository names; this is defence in depth.
    Matching is by whole token (GitHub names cannot contain spaces), so a name
    that is only a substring of a fixed word ("game" in "games") is not a hit.
    """
    exempt = static_tokens() | {t.casefold() for name, _ in stats["languages"] for t in TOKEN_RE.findall(name)}
    for filename, content in outputs.items():
        if "@" in content or any(ord(c) > 127 for c in content):
            raise ValueError(f"{filename}: non-ASCII text or an address marker")
        tokens = visible_tokens(content)
        for index, repo in enumerate(raw["repositories"], 1):
            for name in {repo["name"].casefold(), repo["name"].split("/")[-1].casefold()}:
                if name in tokens and name not in exempt:
                    raise ValueError(f"Privacy guard rejected {filename} for repo#{index}")
                if name in tokens:
                    LOG.info("privacy repo#%d name equals a fixed card word; not a leak", index)


def write_outputs(out, outputs):
    """Stage the entire validated batch before replacing any destination file.

    Roll back existing contents if a replacement fails. Individual replacements
    are atomic; readers should not assume a cross-file filesystem transaction.
    """
    LOG.info("write entry files=%d", len(outputs))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stats-stage-", dir=out) as directory:
        stage = Path(directory)
        originals = {name: (out/name).read_bytes() if (out/name).exists() else None for name in outputs}
        for name, content in outputs.items():
            (stage/name).write_text(content, encoding="utf-8", newline="\n")
        replaced = []
        try:
            for name in outputs:
                os.replace(stage/name, out/name)
                replaced.append(name)
        except OSError:
            for name in reversed(replaced):
                if originals[name] is None:
                    (out/name).unlink()
                else:
                    (stage/name).write_bytes(originals[name])
                    os.replace(stage/name, out/name)
            raise
    for name, content in outputs.items():
        LOG.info("file written %s bytes=%d", name, len(content.encode("utf-8")))
    LOG.info("write exit files=%d", len(outputs))


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--out", type=Path, default=Path("stats"))
    parser.add_argument("--today", type=date.fromisoformat)
    args = parser.parse_args()
    try:
        today = args.today or today_madrid()
        LOG.info("generator entry today=%s mode=%s", today, "fixture" if args.fixture else "live")
        if args.fixture:
            LOG.info("fixture load entry")
            raw = json.loads(args.fixture.read_text(encoding="utf-8"))
            LOG.info("fixture load exit windows=%d repositories=%d private=%d archived=%d", len(raw["windows"]), len(raw["repositories"]), sum(r["isPrivate"] for r in raw["repositories"]), sum(r["isArchived"] for r in raw["repositories"]))
            for i,w in enumerate(raw["windows"],1):
                LOG.info("window#%d from=%s to=%s total=%d sum=%d", i, w["from"], w["to"], w["total"], w["sum"])
                if w["total"] != w["sum"]:
                    LOG.warning("window#%d commit total mismatch", i)
        else:
            raw = fetch(today)
        stats = compute(raw,today)
        LOG.info("render entry themes=2 cards=3")
        outputs = {f"{name}-{theme}.svg": renderer(stats,theme) for theme in THEMES for name,renderer in (("commits",commits_card),("languages",languages_card),("projects",projects_card))}
        privacy_check(raw,stats,outputs)
        LOG.info("render exit files=%d XML and privacy checks passed",len(outputs))
        write_outputs(args.out,outputs)
        LOG.info("generator exit success")
        return 0
    except Exception as exc:
        # Messages are our own (no repository names, no token); KeyError and
        # TypeError name the GraphQL field that was missing, which is what an
        # operator needs to fix a schema change.
        LOG.error("generator failed %s: %s; no SVG was replaced", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
