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


def to_curseforge(body, slugs):
    missing = set()

    def replace(match):
        slug = match.group(1)
        if slug not in slugs:
            missing.add(slug)
            return match.group(0)
        return f"https://www.curseforge.com/minecraft/mc-mods/{slugs[slug]}"

    body = re.sub(r"https://modrinth\.com/(?:mod|project)/([\w-]+)", replace, body)
    body = body.replace("utm_source=modrinth", "utm_source=curseforge")
    if missing:
        fail(f"add {', '.join(sorted(missing))} to curseforge_slugs in {listing_path}")
    leftover = sorted(set(re.findall(r"\S*modrinth\.com\S*", body)))
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
project_id = read_properties("gradle.properties").get("modrinth_project_id")
if not project_id:
    fail("gradle.properties has no modrinth_project_id")

wanted = {"title": name, "description": summary, "body": body}
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

curseforge_body = to_curseforge(body, slugs)
os.makedirs(out_dir, exist_ok=True)
for file_name, content in (("name.txt", name + "\n"), ("summary.txt", summary + "\n"), ("description.md", curseforge_body)):
    with open(os.path.join(out_dir, file_name), "w", encoding="utf-8") as f:
        f.write(content)

if summary_path:
    status = "dry run, nothing sent" if dry_run else ("updated " + ", ".join(changes) if changes else "already up to date")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(f"## Modrinth\n\n{status}\n\n")
        f.write("## CurseForge listing\n\nCurseForge has no API for project details. Paste these into the project settings, or download the `curseforge-listing` artifact.\n\n")
        f.write(f"**Name**\n\n```\n{name}\n```\n\n**Summary**\n\n```\n{summary}\n```\n\n")
        f.write(f"**Description** (Markdown)\n\n`````markdown\n{curseforge_body}`````\n")
