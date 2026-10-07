import difflib
import json
import os
import re
import sys
import urllib.request

listing_path = os.environ.get("LISTING", "LISTING.md")
dry_run = os.environ.get("DRY_RUN", "false") == "true"
out_dir = os.environ.get("OUT_DIR", ".")
summary_path = os.environ.get("GITHUB_STEP_SUMMARY")


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def read_properties(path):
    props = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
    return props


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_listing(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        fail(f"{path} has no frontmatter")
    meta, current = {}, None
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if not isinstance(current, dict):
                fail(f"unexpected indented line in {path} frontmatter: {line}")
            key, _, value = line.strip().partition(":")
            current[key.strip()] = unquote(value)
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value:
            meta[key] = unquote(value)
            current = None
        else:
            current = meta[key] = {}
    return meta, match.group(2).strip() + "\n"


def for_platform(body, platform):
    if body.count("<!-- only:") != body.count("<!-- /only -->"):
        fail(f"unbalanced <!-- only:... --> markers in {listing_path}")

    def keep(match):
        return match.group(2) if match.group(1) == platform else ""

    body = re.sub(r"<!-- only:(\w+) -->\n?(.*?)<!-- /only -->\n?", keep, body, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", body)


def stack_tables(body):
    def stack(match):
        cells = [match.group(1), match.group(2)]
        image = next((cell for cell in cells if "<img" in cell), None)
        text = next((cell for cell in cells if cell is not image), None)
        if image is None or text is None:
            return match.group(0)
        text = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
        heading = re.match(r"(<h\d>.*?</h\d>)\s*(.*)", text, re.S)
        head, rest = (heading.group(1), heading.group(2)) if heading else ("", text)
        parts = [head, "<center>\n" + image.strip() + "\n</center>", rest]
        return "\n\n".join(part for part in parts if part)

    return re.sub(r"<table>\s*<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>\s*</table>", stack, body, flags=re.S)


def to_html(body):
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        fail("the CurseForge copy needs markdown-it-py: pip install -r requirements.txt next to sync_listing.py")
    body = re.sub(r"<details>\s*<summary>(.*?)</summary>", r"<h3>\1</h3>", body, flags=re.S)
    body = re.sub(r"\s*</details>", "", body)
    html = MarkdownIt("commonmark", {"html": True}).enable("table").render(stack_tables(body))
    return html.replace("<center>", '<p style="text-align:center">').replace("</center>", "</p>")


def to_curseforge(body, slugs, modrinth_id, curseforge_id):
    missing = set()

    def replace(match):
        slug = match.group(1)
        if slug not in slugs:
            missing.add(slug)
            return match.group(0)
        return f"https://www.curseforge.com/minecraft/mc-mods/{slugs[slug]}"

    body = re.sub(r"https://modrinth\.com/(?:mod|project)/([\w-]+)", replace, body)
    body = body.replace("utm_source=modrinth", "utm_source=curseforge")
    if curseforge_id:
        body = re.sub(rf"img\.shields\.io/modrinth/(v|dt)/{re.escape(modrinth_id)}\b", rf"img.shields.io/curseforge/\1/{curseforge_id}", body)
    if missing:
        fail(f"add {', '.join(sorted(missing))} to curseforge_slugs in {listing_path}")
    leftover = sorted(set(re.findall(r"\S*(?:modrinth\.com|shields\.io/modrinth)\S*", body)))
    if leftover:
        fail(f"Modrinth links left in the CurseForge description: {', '.join(leftover)}")
    return body


def modrinth(method, path, payload=None):
    request = urllib.request.Request(
        f"https://api.modrinth.com/v2{path}",
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "User-Agent": "Wroud/github-workflows mod-listing",
            "Content-Type": "application/json",
            **({"Authorization": os.environ["MODRINTH_TOKEN"]} if os.environ.get("MODRINTH_TOKEN") else {}),
        },
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


meta, body = parse_listing(listing_path)
name, summary = meta.get("name"), meta.get("summary")
if not name or not summary:
    fail(f"{listing_path} frontmatter needs name and summary")
slugs = meta.get("curseforge_slugs", {})
if not isinstance(slugs, dict):
    fail("curseforge_slugs must be a map of Modrinth slug to CurseForge slug")
properties = read_properties("gradle.properties")
project_id = properties.get("modrinth_project_id")
if not project_id:
    fail("gradle.properties has no modrinth_project_id")

wanted = {"title": name, "description": summary, "body": for_platform(body, "modrinth")}
live = modrinth("GET", f"/project/{project_id}")
changes = {key: value for key, value in wanted.items() if (live.get(key) or "").strip() != value.strip()}

print(f"Modrinth project {live['slug']} ({project_id})")
if not changes:
    print("Modrinth is up to date")
for key, value in changes.items():
    if key == "body":
        diff = difflib.unified_diff((live.get(key) or "").strip().splitlines(), value.strip().splitlines(), "modrinth", listing_path, lineterm="")
        print("body differs:\n" + "\n".join(diff))
    else:
        print(f"{key}: {live.get(key)!r} -> {value!r}")
if changes and not dry_run:
    if not os.environ.get("MODRINTH_TOKEN"):
        fail("MODRINTH_TOKEN is not set")
    modrinth("PATCH", f"/project/{project_id}", changes)
    print(f"Updated {', '.join(changes)} on Modrinth")

curseforge_body = to_html(to_curseforge(for_platform(body, "curseforge"), slugs, project_id, properties.get("curseforge_project_id")))
os.makedirs(out_dir, exist_ok=True)
for file_name, content in (("name.txt", name + "\n"), ("summary.txt", summary + "\n"), ("description.html", curseforge_body)):
    with open(os.path.join(out_dir, file_name), "w", encoding="utf-8") as f:
        f.write(content)

if summary_path:
    status = "dry run, nothing sent" if dry_run else ("updated " + ", ".join(changes) if changes else "already up to date")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(f"## Modrinth\n\n{status}\n\n")
        f.write("## CurseForge listing\n\nCurseForge has no API for project details. Paste these into the project settings, or download the `curseforge-listing` artifact. The description is HTML for the WYSIWYG editor's source view; it has no affiliate banner, which CurseForge only allows at the bottom of the page.\n\n")
        f.write(f"**Name**\n\n```\n{name}\n```\n\n**Summary**\n\n```\n{summary}\n```\n\n")
        f.write(f"**Description** (HTML)\n\n`````html\n{curseforge_body}`````\n")
