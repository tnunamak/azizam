"""Generate the Console comps (self-contained HTML) from real household data."""
import html
import json
from pathlib import Path

OUT = Path.home() / "applications/azizam-wt-design/docs/design/comps/console"
FIX = Path.home() / "applications/azizam-wt-household-seed/scripts/ui/fixtures/household/manifest.json"
MANIFEST = {e["file"]: e for e in json.loads(FIX.read_text())}

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400'
    '&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;500;600'
    '&display=swap" rel="stylesheet">'
)

BASE_CSS = """
:root{
  --bg:#102A2B;--text:#F2F7F6;--accent:#99BFD5;--ok:#8ECBB0;--warn:#E1B975;--fail:#F09B93;
  --line:rgba(242,247,246,.24);--sep:rgba(242,247,246,.16);
  color-scheme:dark;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#F3F7F6;--text:#173332;--accent:#315E80;--ok:#246447;--warn:#815115;--fail:#A03932;
    --line:rgba(23,51,50,.24);--sep:rgba(23,51,50,.16);
    color-scheme:light;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text)}
body{font-family:"IBM Plex Sans",sans-serif;font-size:16px;line-height:24px;font-weight:400;
  -webkit-font-smoothing:antialiased}
a{color:inherit}
img{display:block}
ul{list-style:none}
.display{font-family:"IBM Plex Sans Condensed",sans-serif;font-weight:600;letter-spacing:-.015em}
h1{font-size:28px;line-height:34px;font-weight:600;letter-spacing:-.01em}
h2{font-size:20px;line-height:26px;font-weight:600}
.small{font-size:14px;line-height:20px}
.label{font-size:14px;line-height:20px;font-weight:500}
.num{font-variant-numeric:tabular-nums;font-weight:500}
.mono{font-family:"IBM Plex Mono",monospace;font-size:14px;line-height:20px}
.ic{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.75;
  stroke-linecap:square;stroke-linejoin:miter}
.ic *{vector-effect:non-scaling-stroke}
.ok{color:var(--ok)}.warn{color:var(--warn)}.fail{color:var(--fail)}.unknown{color:var(--accent)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;
  padding:0 16px;border-radius:4px;font:500 16px/24px "IBM Plex Sans",sans-serif;
  text-decoration:none;cursor:pointer;white-space:nowrap}
.btn-primary{background:var(--accent);color:var(--bg);border:1px solid var(--accent)}
.btn-secondary{background:transparent;color:var(--text);border:1px solid var(--accent)}
.btn:focus-visible,a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
.state{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
"""

# 20 x 20 outline icons, squared joins. Each value is the inner SVG markup.
ICONS = {
    "home": '<path d="M3 9 10 3l7 6v8h-5v-5H8v5H3z"/>',
    "key": '<circle cx="6.5" cy="13.5" r="3.5"/><path d="M9 11l7-7M13 7l2 2M15 5l2 2"/>',
    "library": '<rect x="3" y="4" width="14" height="12"/><path d="M3 13l5-4 4 4 2-2 3 3"/><circle cx="13" cy="7.5" r="1.2"/>',
    "try": '<path d="M3 4h14v9H9l-4 3.5V13H3z"/>',
    "status": '<path d="M10 2.5 16.5 5v5c0 4-3 6.5-6.5 7.5C6.5 16.5 3.5 14 3.5 10V5z"/>',
    "engines": '<rect x="3" y="3.5" width="14" height="5"/><rect x="3" y="11.5" width="14" height="5"/><path d="M6 6h1M6 14h1"/>',
    "providers": '<path d="M6 15h8.5a3 3 0 0 0 0-6 4.5 4.5 0 0 0-8.5-.5A3.25 3.25 0 0 0 6 15z"/>',
    "people": '<circle cx="7.5" cy="7" r="2.75"/><path d="M2.5 16.5c0-3.5 2.5-5 5-5s5 1.5 5 5"/><circle cx="13.75" cy="6.5" r="2.25"/><path d="M14 11.5c2 .1 3.5 1.5 3.5 5"/>',
    "usage": '<circle cx="10" cy="10" r="7.25"/><path d="M12.5 7.25H8.75a1.375 1.375 0 0 0 0 2.75h2.5a1.375 1.375 0 0 1 0 2.75H7.5M10 5.5v1.75M10 12.75v1.75"/>',
    "activity": '<path d="M7 5.5h10M7 10h10M7 14.5h10M3 5.5h1M3 10h1M3 14.5h1"/>',
    "settings": '<path d="M3 6h14M3 14h14"/><rect x="5" y="4" width="4" height="4" fill="var(--bg)"/><rect x="11" y="12" width="4" height="4" fill="var(--bg)"/>',
    "more": '<path d="M4 10h.5M10 10h.5M16 10h.5" stroke-width="2.5"/>',
    "search": '<circle cx="8.5" cy="8.5" r="5"/><path d="M12.5 12.5 17 17"/>',
    "chev-down": '<path d="M5 7.5l5 5 5-5"/>',
    "chev-right": '<path d="M7.5 5l5 5-5 5"/>',
    "check": '<path d="M4 10.5l4 4 8-9"/>',
    "cross": '<path d="M5 5l10 10M15 5 5 15"/>',
    "triangle": '<path d="M10 3 17.5 16.5h-15z"/><path d="M10 8.5v3.5M10 14v.5"/>',
    "question": '<circle cx="10" cy="10" r="7.25"/><path d="M7.75 8a2.25 2.25 0 1 1 3.1 2.1c-.6.3-.85.7-.85 1.4v.5M10 14v.5"/>',
    "pause": '<path d="M7.5 5v10M12.5 5v10"/>',
    "download": '<path d="M10 3v10M6 9l4 4 4-4M4 16.5h12"/>',
    "receipt": '<path d="M5 2.5h10v15L12.5 16 10 17.5 7.5 16 5 17.5z"/><path d="M8 7h4M8 10h4"/>',
    "copy": '<rect x="7" y="7" width="10" height="10"/><path d="M13 7V3H3v10h4"/>',
    "audio": '<path d="M4 9v2M7 6.5v7M10 4v12M13 6.5v7M16 9v2"/>',
    "play": '<path d="M6 4l10 6-10 6z"/>',
}


def icon(name, cls="", size=20, label=None):
    aria = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    return (f'<svg class="ic {cls}" width="{size}" height="{size}" viewBox="0 0 20 20" {aria}>'
            f'{ICONS[name]}</svg>')


STATE = {  # word -> (icon, colour class)
    "ok": ("check", "ok"),
    "attention": ("triangle", "warn"),
    "failed": ("cross", "fail"),
    "unknown": ("question", "unknown"),
    "paused": ("pause", ""),
}


def state(kind, words):
    ic, cls = STATE[kind]
    return f'<span class="state {cls}">{icon(ic)}<span>{words}</span></span>'


def page(title, css, body, lang="en"):
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
{FONTS}
<style>{BASE_CSS}{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


# ---------------------------------------------------------------- desktop shell
SHELL_CSS = """
.shell{display:grid;grid-template-columns:208px 1fr;min-height:100vh}
.side{border-right:1px solid var(--line);padding:0 12px 24px}
.brand{height:56px;display:flex;align-items:center;padding:0 12px;font-size:20px;line-height:26px;font-weight:600}
.nav a,.nav button{display:flex;align-items:center;gap:12px;width:100%;min-height:44px;padding:0 12px;
  border-radius:4px;text-decoration:none;color:var(--text);font:400 16px/24px "IBM Plex Sans",sans-serif;
  background:none;border:1px solid transparent;cursor:pointer;text-align:left}
.nav a[aria-current="page"]{border-color:var(--accent);font-weight:600}
.nav .group{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.nav .group button .ic:last-child{margin-left:auto}
.main{min-width:0}
.rail{height:56px;border-bottom:1px solid var(--line);display:flex;align-items:center;
  justify-content:space-between;padding:0 24px}
.search{display:flex;align-items:center;gap:8px;width:320px;height:44px;padding:0 12px;
  border:1px solid var(--accent);border-radius:4px}
.search input{all:unset;flex:1;font-size:16px;line-height:24px;color:var(--text)}
.search:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.search input:focus-visible{outline:none}
.search input::placeholder{color:var(--text);opacity:1}
.account{display:flex;align-items:center;gap:12px;min-height:44px;padding:0 8px;border:1px solid transparent;
  background:none;color:var(--text);font:500 16px/24px "IBM Plex Sans",sans-serif;cursor:pointer}
.avatar{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--text);border-radius:4px;
  font-size:14px;line-height:20px;font-weight:600}
.content{padding:24px}
"""

NAV_MAIN = [("home", "Home"), ("key", "Apps & keys"), ("try", "Try"), ("library", "Library")]
NAV_ADMIN = [("status", "Status"), ("engines", "Engines"), ("providers", "Providers"), ("people", "People"),
             ("usage", "Usage"), ("activity", "Activity"), ("settings", "Settings")]


def shell(current, admin_open, content, search_label="Search"):
    def link(ic, name):
        cur = ' aria-current="page"' if name == current else ""
        return f'<li><a href="#"{cur}>{icon(ic)}<span>{name}</span></a></li>'

    main = "".join(link(i, n) for i, n in NAV_MAIN)
    chev = "chev-down" if admin_open else "chev-right"
    admin = "".join(link(i, n) for i, n in NAV_ADMIN) if admin_open else ""
    return f"""<div class="shell">
<aside class="side">
  <div class="brand">Azizam</div>
  <nav class="nav" aria-label="Main">
    <ul>{main}</ul>
    <div class="group">
      <button type="button" aria-expanded="{str(admin_open).lower()}"><span>Admin</span>{icon(chev)}</button>
      <ul>{admin}</ul>
    </div>
  </nav>
</aside>
<div class="main">
  <div class="rail">
    <label class="search">{icon("search")}<span class="vh">{search_label}</span><input placeholder="{search_label}"></label>
    <button class="account" type="button" aria-label="Account, Sam"><span class="avatar">S</span><span>Sam</span>{icon("chev-down")}</button>
  </div>
  <main class="content">
{content}
  </main>
</div>
</div>"""


# ---------------------------------------------------------------- status
STATUS_CSS = """
.incident{border:1px solid var(--line);border-radius:8px;padding:24px;margin-top:16px}
.verdict{display:flex;align-items:center;gap:12px;font-size:40px;line-height:44px}
.verdict .ic{width:32px;height:32px}
.facts{display:grid;grid-template-columns:168px 1fr;gap:8px 24px;max-width:760px;margin-top:16px}
.facts dt{font-size:14px;line-height:20px;font-weight:500;align-self:center}
.facts dd{align-self:center}
.incident .act{margin-top:24px}
.evidence-row{display:flex;align-items:center;gap:16px;margin-top:16px;padding-top:16px;border-top:1px solid var(--sep)}
.lower{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:48px;margin-top:32px}
.paths{width:100%;border-collapse:collapse;margin-top:12px}
.paths th,.paths td{padding:12px 16px;text-align:left;height:48px}
.paths td:first-child,.paths td:last-child{white-space:nowrap}
.paths th:first-child,.paths td:first-child{padding-left:0}
.paths thead th{font-size:14px;line-height:20px;font-weight:500;border-bottom:1px solid var(--sep)}
.reading{display:flex;align-items:baseline;gap:12px;margin-top:24px;flex-wrap:wrap}
.reading .big{font-size:24px;line-height:28px}
.usage-link{display:flex;align-items:center;gap:16px;margin-top:16px}
.history h2+p{margin-top:8px}
.changes{margin-top:12px}
.changes li{padding:12px 0}
.changes li+li{border-top:1px solid var(--sep)}
.history .btn{margin-top:8px}
"""


def status_html():
    paths = [
        ("Gateway", "Gateway", "All requests", state("ok", "Reachable")),
        ("Execution gateway (chat)", "Engine", "Chat", state("ok", "Ready")),
        ("Study and coding (chat)", "Engine", "Chat", state("ok", "Ready")),
        ("Image studio (images)", "Engine", "Images", state("ok", "Ready")),
        ("Home Assistant voice (music &amp; sound effects)", "Engine", "Music, Sound effects", state("ok", "Ready")),
        ("Document search (embeddings)", "Engine", "Embeddings", state("paused", "Paused")),
        ("Remote backup", "Provider", "Chat", state("unknown", "We haven't checked yet")),
    ]
    prow = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in paths)
    content = f"""
<h1>Status</h1>
<section class="incident" aria-labelledby="verdict">
  <p class="verdict display" id="verdict">{icon("cross", "fail", 32, "Failed")}Video is unavailable</p>
  <dl class="facts">
    <dt>Where it stopped</dt><dd>Engine: Video studio (video)</dd>
    <dt>Who is affected</dt><dd>Everyone who makes video. It is the only engine that serves Video.</dd>
  </dl>
  <a class="btn btn-primary act" href="#">Check engine</a>
  <p class="evidence-row"><span><span class="label">Last failed request</span><br>Video, from Open WebUI, 21h ago</span><a class="btn btn-secondary" href="#">{icon("receipt")}Open receipt</a></p>
</section>

<div class="lower">
  <section aria-labelledby="paths">
    <h2 id="paths">Other capabilities and paths</h2>
    <table class="paths">
      <thead><tr><th scope="col">Name</th><th scope="col">Layer</th><th scope="col">Serves</th><th scope="col">State now</th></tr></thead>
      <tbody>{prow}</tbody>
    </table>
    <p class="reading"><span class="label">Provider spend this month</span><span class="num big">USD 0.45</span><span>of USD 8.00. Cost recorded for 249 of 267 requests.</span></p>
    <p class="usage-link"><span>Request volume by day is on Usage.</span><a class="btn btn-secondary" href="#">Open usage</a></p>
  </section>
  <section class="history" aria-labelledby="resolved">
    <h2 id="resolved">Resolved incidents</h2>
    <p>No resolved incidents saved.</p>
    <h2 style="margin-top:32px">Recent changes</h2>
    <ul class="changes">
      <li>Updated engine configuration<br><span class="small">System, 2m ago</span></li>
      <li>Turned off an engine<br><span class="small">Sam, 4d ago</span></li>
      <li>Updated provider settings<br><span class="small">Sam, 12d ago</span></li>
    </ul>
    <a class="btn btn-secondary" href="#">Open activity</a>
  </section>
</div>
"""
    return page("Status · Azizam (Console comp)", SHELL_CSS + STATUS_CSS, shell("Status", True, content))


# ---------------------------------------------------------------- library
LIB = [  # (file, time) in the order of the owner Library page
    ("kitchen-morning.webp", "just now"), ("garden-plan.webp", "11h ago"), ("school-ecosystem.webp", "22h ago"),
    ("dog-window.webp", "1d ago"), ("coastal-weekend.webp", "2d ago"), ("reading-nook.webp", "2d ago"),
    ("solar-project.webp", "3d ago"), ("apple-tart.webp", "3d ago"), ("cat-bookshelf.webp", "4d ago"),
    ("mountain-cabin.webp", "4d ago"), ("wildflower-study.webp", "5d ago"), ("herb-window.webp", "5d ago"),
    ("birthday-table.webp", "6d ago"), ("train-journey.webp", "6d ago"), ("birdhouse.webp", "6d ago"),
    ("rainy-patio.webp", "7d ago"), ("picnic-lake.webp", "7d ago"), ("garden-evening.webp", "8d ago"),
    ("garden-reminder.mp3", "8d ago"), ("school-intro.mp3", "9d ago"), ("weekend-plan.mp3", "9d ago"),
    ("welcome-june.mp3", "10d ago"), ("recipe-note.mp3", "10d ago"), ("garden-breeze.mp4", "11d ago"),
]
DUR = {"garden-reminder.mp3": "0:04", "school-intro.mp3": "0:05", "weekend-plan.mp3": "0:05",
       "welcome-june.mp3": "0:06", "recipe-note.mp3": "0:07", "garden-breeze.mp4": "0:03"}

LIB_CSS = """
.lib-head{display:flex;align-items:center;justify-content:space-between;gap:24px}
.toolbar{display:flex;align-items:center;gap:16px}
.toolbar .search{width:280px}
.field{display:flex;align-items:center;gap:8px}
.selwrap{position:relative;display:inline-flex}
.selwrap select{appearance:none;-webkit-appearance:none;height:44px;min-width:150px;padding:0 40px 0 12px;
  border:1px solid var(--accent);border-radius:4px;background:var(--bg);color:var(--text);
  font:400 16px/24px "IBM Plex Sans",sans-serif;cursor:pointer}
.selwrap .ic{position:absolute;right:10px;top:12px;pointer-events:none}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 384px;gap:24px;margin-top:24px;align-items:start}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.grid a{display:block;padding:6px;border:2px solid transparent;border-radius:4px;text-decoration:none}
.grid a[aria-current="true"]{border-color:var(--accent)}
.frame{aspect-ratio:1/1;width:100%;overflow:hidden;border-radius:0}
.frame img{width:100%;height:100%;object-fit:cover}
.frame.audio{display:grid;place-items:center;border:1px solid var(--line)}
.frame.audio .ic{width:40px;height:40px}
.ctitle{margin-top:8px;font-size:14px;line-height:20px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.grid a[aria-current="true"] .ctitle{font-weight:500}
.cmeta{display:block;font-size:14px;line-height:20px}
.detail{border:1px solid var(--line);border-radius:8px;padding:16px}
.detail img{width:350px;height:350px;object-fit:cover;border-radius:0}
.detail h2{margin-top:16px}
.detail .prompt{margin-top:8px}
.detail dl{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;margin-top:16px}
.detail dt{font-size:14px;line-height:20px;font-weight:500;padding-top:2px}
.detail .actions{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.detail .actions .btn:first-child{grid-column:1 / -1}
.detail .cost{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
"""


def lib_cell(f, t, selected):
    e = MANIFEST[f]
    text = html.escape(e["prompt"])
    if e["kind"] == "image":
        frame = f'<span class="frame"><img src="assets/{f}" alt="" width="768" height="768"></span>'
        meta = t
    elif e["kind"] == "video":
        frame = '<span class="frame"><img src="assets/garden-breeze-poster.webp" alt="" width="768" height="512"></span>'
        meta = f"Video, {DUR[f]}, {t}"
    else:
        frame = f'<span class="frame audio">{icon("audio")}</span>'
        meta = f"Audio, {DUR[f]}, {t}"
    cur = ' aria-current="true"' if selected else ""
    return (f'<li><a href="#"{cur}>{frame}<span class="ctitle">{text}</span>'
            f'<span class="cmeta">{meta}</span></a></li>')


def select(label, sid, options):
    opts = "".join(f"<option>{o}</option>" for o in options)
    return (f'<div class="field"><label class="label" for="{sid}">{label}</label>'
            f'<span class="selwrap"><select id="{sid}">{opts}</select>{icon("chev-down")}</span></div>')


def library_html():
    sel = MANIFEST["kitchen-morning.webp"]
    cells = "".join(lib_cell(f, t, i == 0) for i, (f, t) in enumerate(LIB))
    content = f"""
<div class="lib-head">
  <h1>Library</h1>
  <div class="toolbar">
    <label class="search">{icon("search")}<span class="vh">Search creations</span><input placeholder="Search creations"></label>
    {select("Type", "f-type", ["All kinds", "Images", "Video", "Audio"])}
    {select("Person", "f-person", ["Everyone", "Sam", "Robin", "Alex"])}
  </div>
</div>
<div class="cols">
  <nav aria-label="Creations, newest first"><ul class="grid">{cells}</ul></nav>
  <aside class="detail" aria-label="Selected creation">
    <img src="assets/kitchen-morning.webp" alt="{html.escape(sel['prompt'])}" width="350" height="350">
    <h2>Image</h2>
    <p class="prompt">{html.escape(sel['prompt'])}</p>
    <dl>
      <dt>File</dt><dd>WebP, <span class="num">768 × 768</span></dd>
      <dt>Made by</dt><dd>Sam, just now</dd>
    </dl>
    <div class="actions">
      <a class="btn btn-primary" href="#">{icon("download")}Download</a>
      <a class="btn btn-secondary" href="#">{icon("copy")}Copy prompt</a>
      <a class="btn btn-secondary" href="#">{icon("receipt")}Open receipt</a>
    </div>
    <p class="cost">Cost not recorded</p>
  </aside>
</div>
"""
    return page("Library · Azizam (Console comp)", SHELL_CSS + LIB_CSS, shell("Library", False, content))


# ---------------------------------------------------------------- home (phone, member Robin)
HOME_CSS = """
.top{height:56px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;
  border-bottom:1px solid var(--line)}
.top .brand{font-size:20px;line-height:26px;font-weight:600}
.acct{width:44px;height:44px;display:grid;place-items:center;background:none;border:0;color:var(--text);cursor:pointer}
.avatar{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--text);border-radius:4px;
  font-size:14px;line-height:20px;font-weight:600}
main{padding:16px 16px calc(64px + env(safe-area-inset-bottom) + 24px)}
h1{font-size:26px;line-height:32px}
.task{margin-top:12px;border:1px solid var(--line);border-radius:8px;padding:16px}
.ready{font-size:30px;line-height:34px;display:flex;align-items:center;gap:8px}
.ready .ic{width:24px;height:24px}
.evidence{margin-top:4px}
.task .primary{min-height:48px;width:100%;margin-top:16px}
.also{margin-top:16px;padding-top:8px;border-top:1px solid var(--sep)}
.also li{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:52px}
.also li+li{border-top:1px solid var(--sep)}
.also .btn{min-height:44px;min-width:164px;justify-content:flex-start}
.also .note{display:grid;grid-template-columns:20px 1fr;gap:12px;align-items:start;padding:12px 0}
.also .note .ic{margin-top:2px}
.connect{min-height:48px;width:100%;margin-top:16px}
.block{margin-top:24px}
.block-head{display:flex;align-items:center;justify-content:space-between;min-height:44px}
.rows li{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:48px}
.rows li+li{border-top:1px solid var(--sep)}
.recent{display:grid;grid-template-columns:64px 1fr auto;gap:12px;align-items:center;margin-top:8px}
.recent img{width:64px;height:64px;object-fit:cover}
.recent .small{display:block}
.recent .t{font-size:14px;line-height:20px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tabbar{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);
  display:grid;grid-template-columns:repeat(4,1fr);height:calc(64px + env(safe-area-inset-bottom));
  padding-bottom:env(safe-area-inset-bottom)}
.tabbar a{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  text-decoration:none;font-size:14px;line-height:20px;min-height:44px}
.tabbar a .ic{width:24px;height:24px}
.tabbar a[aria-current="page"]{font-weight:600;color:var(--accent)}
.tabbar a[aria-current="page"] .ic{stroke-width:2.25}
body.capture-full .tabbar{position:static}
body.capture-full main{padding-bottom:24px}
"""

ALSO = [("Images", "Make an image"), ("Speech", "Read aloud"), ("Transcription", "Transcribe audio"),
        ("Music", "Make music"), ("Sound effects", "Make a sound")]


def home_html():
    garden = MANIFEST["garden-plan.webp"]["prompt"]
    tabs = [("home", "Home", True), ("key", "Apps & keys", False), ("library", "Library", False), ("more", "More", False)]
    tabbar = "".join(
        f'<a href="#"{" aria-current=\"page\"" if c else ""}>{icon(i, size=24)}<span>{n}</span></a>' for i, n, c in tabs)
    also = "".join(f'<li><span class="state">{icon("check", "ok", label="Ready")}<span>{cap}</span></span>'
                   f'<a class="btn btn-secondary" href="#">{act}</a></li>' for cap, act in ALSO)
    body = f"""<header class="top">
  <span class="brand">Azizam</span>
  <button class="acct" type="button" aria-label="Account, Robin"><span class="avatar">R</span></button>
</header>
<main>
  <h1>Home</h1>
  <section class="task" aria-labelledby="ready">
    <p class="ready display" id="ready">{icon("check", "ok", 24, "Ready")}Chat is ready.</p>
    <p class="small evidence">Last used 20h ago</p>
    <a class="btn btn-primary primary" href="#">Open chat</a>
    <h2 class="vh" id="also-h">Also ready</h2>
    <ul class="also" aria-labelledby="also-h">
      {also}
      <li class="note">{icon("cross", "fail", label="Unavailable")}<p>Video isn't working right now. Ask Sam if you need it.</p></li>
      <li class="note">{icon("pause", label="Paused")}<p>Embeddings are paused. Ask Sam to turn them back on.</p></li>
    </ul>
  </section>
  <a class="btn btn-secondary connect" href="#">Connect an app</a>

  <section class="block" aria-labelledby="apps">
    <div class="block-head"><h2 id="apps">Your apps</h2><a class="btn btn-secondary" href="#">View all</a></div>
    <ul class="rows">
      <li><span>Robin's personal app</span><span class="small">Last used 1h ago</span></li>
      <li><span>Open WebUI</span><span class="small">Last used 4h ago</span></li>
      <li><span>Telegram helper</span><span class="small">Not used yet</span></li>
    </ul>
  </section>

  <section class="block" aria-labelledby="recent">
    <h2 id="recent">Recent creation</h2>
    <div class="recent">
      <img src="assets/garden-plan.webp" alt="" width="64" height="64">
      <div><span class="t">{html.escape(garden)}</span><span class="small">11h ago</span></div>
      <a class="btn btn-secondary" href="#">Open</a>
    </div>
  </section>
</main>
<nav class="tabbar" aria-label="Main">{tabbar}</nav>"""
    return page("Home · Azizam (Console comp)", HOME_CSS, body)


(OUT / "home.html").write_text(home_html())
(OUT / "status.html").write_text(status_html())
(OUT / "library.html").write_text(library_html())
print("ok")
