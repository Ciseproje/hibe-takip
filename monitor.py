#!/usr/bin/env python3
"""Hibe/fon/STK ilan takipcisi.
Kullanim:
  python monitor.py daily            # yeni ilanlari mail atar
  python monitor.py weekly           # son 7 gunun en iyilerini mail atar
  python monitor.py daily --dry-run  # mail atmaz, ekrana yazar
"""
import sys, os, re, json, hashlib, smtplib, datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse
import requests, yaml
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HibeTakipBot/1.0)"}
DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b")


def tr_lower(s):
    return s.replace("I", "ı").replace("İ", "i").lower()


def load_yaml(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def score_item(text, cfg):
    t = tr_lower(text)
    score, tags = 0, []
    for k, w in cfg["keywords"].items():
        if k in t:
            score += w
            tags.append(k)
    for k, w in cfg["negative"].items():
        if k in t:
            score += w
    aud_hits = {}
    for name, a in cfg["audiences"].items():
        if any(w in t for w in a["words"]):
            aud_hits[name] = a["weight"]
    audience = max(aud_hits, key=aud_hits.get) if aud_hits else "Genel"
    if aud_hits and score > 0:
        score += aud_hits[audience]
    return score, audience, tags[:4]


def extract(source, cfg):
    r = requests.get(source["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < 15:
            continue
        href = urljoin(source["url"], a["href"])
        if urlparse(href).scheme not in ("http", "https") or href in seen:
            continue
        seen.add(href)
        parent = a.find_parent(["li", "article", "div", "tr"])
        ctx = " ".join(parent.get_text(" ", strip=True).split())[:300] if parent else title
        score, audience, tags = score_item(title + " " + ctx, cfg)
        if score < cfg["min_score"]:
            continue
        dates = [f"{d}.{m}.{y}" for d, m, y in DATE_RE.findall(ctx)][:2]
        out.append({
            "id": hashlib.sha1(href.encode()).hexdigest()[:16],
            "title": title[:200], "url": href, "source": source["name"],
            "category": source["category"], "audience": audience,
            "score": score, "tags": tags, "dates": dates,
        })
    return out


def render(items, title, errors):
    order = {"STK": 0, "Bireysel": 1, "Kurumsal": 2, "Genel": 3}
    groups = {}
    for it in items:
        groups.setdefault(it["audience"], []).append(it)
    html = [f"<h2>{title}</h2>"]
    if not items:
        html.append("<p>Kriterlere uyan yeni kayıt bulunamadı.</p>")
    for aud in sorted(groups, key=lambda a: order.get(a, 9)):
        html.append(f"<h3>{aud} başvuruları</h3><ul>")
        for it in sorted(groups[aud], key=lambda x: -x["score"]):
            d = f" — <i>tarih: {', '.join(it['dates'])}</i>" if it["dates"] else ""
            html.append(
                f"<li><a href='{it['url']}'>{it['title']}</a><br>"
                f"<small>{it['source']} · {it['category']} · puan {it['score']}{d}</small></li>")
        html.append("</ul>")
    if errors:
        html.append("<hr><small>Taranamayan kaynaklar: " + ", ".join(errors) + "</small>")
    return "\n".join(html)


def send_mail(subject, html, dry):
    if dry or not os.environ.get("GMAIL_APP_PASSWORD"):
        print(subject, "\n", html)
        return
    user = os.environ["GMAIL_USER"]
    to = os.environ.get("MAIL_TO", user)
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    dry = "--dry-run" in sys.argv
    cfg, srcs = load_yaml("config.yml"), load_yaml("sources.yml")["sources"]
    state = load_state()
    today = dt.date.today()
    errors, new_items = [], []

    for s in srcs:
        try:
            for it in extract(s, cfg):
                if it["id"] not in state["items"]:
                    it["first_seen"] = today.isoformat()
                    state["items"][it["id"]] = it
                    new_items.append(it)
        except Exception as e:
            errors.append(f"{s['name']} ({type(e).__name__})")

    if mode == "weekly":
        cutoff = (today - dt.timedelta(days=7)).isoformat()
        week = [i for i in state["items"].values() if i["first_seen"] >= cutoff]
        week = sorted(week, key=lambda x: -x["score"])[:cfg["weekly_top_n"]]
        send_mail(f"Haftalık Hibe & Fon Özeti – {today:%d.%m.%Y}",
                  render(week, "Haftanın en uygun fırsatları", errors), dry)
    else:
        top = sorted(new_items, key=lambda x: -x["score"])[:cfg["daily_max"]]
        send_mail(f"Günlük Hibe & Fon Özeti – {today:%d.%m.%Y}",
                  render(top, "Bugünün yeni kayıtları", errors), dry)

    # eski kayitlari temizle (90 gunden eski)
    old = (today - dt.timedelta(days=90)).isoformat()
    state["items"] = {k: v for k, v in state["items"].items() if v["first_seen"] >= old}
    if not dry:
        save_state(state)


if __name__ == "__main__":
    main()
