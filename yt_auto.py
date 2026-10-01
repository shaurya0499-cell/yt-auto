#!/usr/bin/env python3
"""Free YouTube automation: daily commentary Short + weekly long-form recap.

Usage:
    python yt_auto.py short            # make + upload today's Short
    python yt_auto.py weekly           # make + upload the weekly recap
    python yt_auto.py short --dry-run  # make the video only, no upload
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import edge_tts
import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
HISTORY = ROOT / "history.json"
OUT = ROOT / "out"


# ----------------------------------------------------------------- helpers
def load_cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"used_links": [], "week": []}


def save_history(h):
    h["used_links"] = h["used_links"][-300:]
    cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    h["week"] = [w for w in h["week"] if w["date"] >= cutoff]
    HISTORY.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    if p.returncode:
        sys.exit(f"Command failed: {' '.join(map(str, cmd))}\n{p.stderr[-1500:]}")
    return p.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path])
    return float(out.strip())


# --------------------------------------------------------------------- LLM
def gemini_json(prompt, cfg):
    key = os.environ["GEMINI_API_KEY"]
    model = cfg["llm"]["model"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    last = ""
    for attempt in range(4):
        r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=180)
        if r.ok:
            try:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                return json.loads(text)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                last = f"bad response: {e}"
        else:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
        time.sleep(5 * (attempt + 1))
    sys.exit(f"Gemini failed: {last}")


# ------------------------------------------------------------------ topics
def fetch_topics(cfg, used):
    items = []
    for url in cfg["sources"]:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
        for e in feed.entries[:15]:
            link = e.get("link")
            if link and link not in used:
                summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))[:400]
                items.append({"title": e.get("title", ""), "summary": summary, "link": link})
    random.shuffle(items)
    return items[:8]


def make_short_plan(cfg, topics, recent_titles):
    ch = cfg["channel"]
    listing = "\n".join(f"{i}. {t['title']} - {t['summary']}" for i, t in enumerate(topics))
    prompt = f"""You write scripts for a YouTube Shorts channel.
Niche: {ch['niche']}. Tone: {ch['tone']}. Language: {ch['language']}.

Candidate topics:
{listing}

Pick the ONE most engaging topic and write a commentary Short about it.
Rules:
- About {cfg['short']['target_words']} spoken words (40-50 seconds).
- The first sentence is a hook that creates curiosity or a strong opinion.
- Give your own take and one concrete insight. Do not copy phrasing from the source.
- Only state facts present in the topic text. No invented numbers or quotes.
- End with a short question inviting comments.
- Plain spoken text only: no emojis, hashtags, or stage directions.
Recently covered (avoid similar angles): {recent_titles}

Return JSON: {{"pick": <topic index>, "title": "<max 70 chars, no hashtags>",
"description": "<2-3 sentences>", "tags": ["..."], "script": "...",
"keywords": ["4-6 concrete, filmable stock-footage search terms"]}}"""
    return gemini_json(prompt, cfg)


def make_weekly_plan(cfg, items):
    ch = cfg["channel"]
    target = cfg["weekly"]["target_words"]
    listing = "\n".join(f"- {w['title']}: {w['script']}" for w in items)
    prompt = f"""You write long-form YouTube videos.
Niche: {ch['niche']}. Tone: {ch['tone']}. Language: {ch['language']}.

Write a weekly recap video of about {target} spoken words (roughly {target // 150} minutes)
covering these topics from this week's Shorts. Add deeper context, your own analysis,
and a takeaway or prediction for each topic.

Topics:
{listing}

Structure: intro hook, one segment per topic, outro asking viewers to comment and subscribe.
Rules: only facts present in the material above; no invented stats or quotes;
plain spoken text, no emojis, hashtags, or stage directions.

Return JSON: {{"title": "<max 80 chars>", "description": "<3-4 sentences>", "tags": ["..."],
"segments": [{{"label": "<2-4 word chapter name>", "text": "...",
"keywords": ["4-6 concrete, filmable stock-footage search terms"]}}]}}"""
    return gemini_json(prompt, cfg)


# --------------------------------------------------------------------- TTS
async def synth(text, voice, out):
    comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
    return words


# ------------------------------------------------------------------ visuals
def pexels_clip(keyword, orientation, min_w, used, wd):
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": key},
                         params={"query": keyword, "orientation": orientation, "per_page": 10},
                         timeout=30)
    except requests.RequestException:
        return None
    if not r.ok:
        return None
    vids = [v for v in r.json().get("videos", []) if v["id"] not in used]
    random.shuffle(vids)
    for v in vids:
        files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and f.get("width")]
        if not files:
            continue
        ok = [f for f in files if f["width"] >= min_w]
        f = min(ok, key=lambda x: x["width"]) if ok else max(files, key=lambda x: x["width"])
        path = wd / f"pexels_{v['id']}.mp4"
        try:
            with requests.get(f["link"], stream=True, timeout=90) as d:
                d.raise_for_status()
                with open(path, "wb") as fh:
                    for c in d.iter_content(1 << 20):
                        fh.write(c)
        except requests.RequestException:
            continue
        used.add(v["id"])
        return path
    return None


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(words, path, w, h, fontsize, margin_v, per=3):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3, fontsize // 14)},2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for i in range(0, len(words), per):
        chunk = words[i:i + per]
        start = chunk[0][0]
        end = words[i + per][0] if i + per < len(words) else chunk[-1][1]
        text = re.sub(r"[{}\\]", "", " ".join(c[2] for c in chunk)).upper()
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    path.write_text(head + "\n".join(lines), encoding="utf-8")


def even_words(text, dur):
    toks = text.split()
    step = dur / max(1, len(toks))
    return [(i * step, (i + 1) * step, t) for i, t in enumerate(toks)]


def build_segment(text, keywords, cfg, wd, idx, w, h, orientation, min_w,
                  used, fontsize, margin_v):
    """Voiceover + stock footage + burned captions for one block of script."""
    audio = wd / f"a{idx}.mp3"
    words = asyncio.run(synth(text, cfg["channel"]["voice"], audio))
    dur = probe(audio)
    if not words:
        words = even_words(text, dur)

    kws = list(keywords) + list(cfg["channel"].get("fallback_keywords", []))
    n = max(1, round(dur / cfg["video"]["clip_seconds"]))
    piece_dur = dur / n
    pieces = []
    for i in range(n):
        clip = None
        for j in range(min(4, len(kws))):
            clip = pexels_clip(kws[(i + j) % len(kws)], orientation, min_w, used, wd)
            if clip:
                break
        piece = wd / f"p{idx}_{i}.mp4"
        if clip:
            run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", clip, "-t", f"{piece_dur:.3f}",
                 "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-pix_fmt", "yuv420p", piece])
        else:  # no footage found: plain dark background
            run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                 f"color=c=0x111827:s={w}x{h}:d={piece_dur:.3f}:r=30",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", piece])
        pieces.append(piece)

    lst = wd / f"list{idx}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in pieces))
    visual = wd / f"v{idx}.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", visual])

    write_ass(words, wd / f"cap{idx}.ass", w, h, fontsize, margin_v)
    seg = wd / f"seg{idx}.mp4"
    run(["ffmpeg", "-y", "-i", visual, "-i", audio, "-vf", f"ass=cap{idx}.ass",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", "-shortest", seg],
        cwd=wd)
    return seg, dur


# ------------------------------------------------------------------ upload
def upload(path, title, description, tags, cfg, privacy):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t[:30] for t in tags][:15],
            "categoryId": str(cfg["youtube"]["category_id"]),
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded: https://youtu.be/" + resp["id"])


def hashtags(tags, n=3):
    return " ".join("#" + re.sub(r"\W", "", t) for t in tags[:n] if re.sub(r"\W", "", t))


# ---------------------------------------------------------------- commands
def cmd_short(cfg, dry):
    h = load_history()
    topics = fetch_topics(cfg, set(h["used_links"]))
    if not topics:
        sys.exit("No fresh topics found. Add more RSS feeds under `sources` in config.yaml.")
    plan = make_short_plan(cfg, topics, [w["title"] for w in h["week"]][-10:])
    topic = topics[int(plan.get("pick", 0)) % len(topics)]
    today = dt.date.today().isoformat()
    OUT.mkdir(exist_ok=True)
    final = OUT / f"short_{today}.mp4"

    with tempfile.TemporaryDirectory() as td:
        seg, _ = build_segment(plan["script"], plan["keywords"], cfg, Path(td), 0,
                               1080, 1920, "portrait", 720, set(), 96, 520)
        shutil.copy(seg, final)
    print("Rendered", final)

    desc = (f"{plan['description']}\n\nSource: {topic['link']}\n\n"
            f"#Shorts {hashtags(plan['tags'])}\n\nStock footage: Pexels")
    if not dry:
        upload(final, plan["title"], desc, plan["tags"], cfg, cfg["short"]["privacy"])
    h["used_links"].append(topic["link"])
    h["week"].append({"date": today, "title": plan["title"], "script": plan["script"],
                      "link": topic["link"]})
    save_history(h)


def fmt_ts(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def cmd_weekly(cfg, dry):
    h = load_history()
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    items = [w for w in h["week"] if w["date"] > cutoff]
    if not items:
        sys.exit("No Shorts from the past week in history.json yet. Run `short` first.")
    plan = make_weekly_plan(cfg, items)
    today = dt.date.today().isoformat()
    OUT.mkdir(exist_ok=True)
    final = OUT / f"weekly_{today}.mp4"

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        used, segs, chapters, t = set(), [], [], 0.0
        for i, s in enumerate(plan["segments"]):
            seg, dur = build_segment(s["text"], s["keywords"], cfg, wd, i,
                                     1920, 1080, "landscape", 1280, used, 64, 90)
            segs.append(seg)
            chapters.append(f"{fmt_ts(t)} {s['label']}")
            t += dur
        lst = wd / "all.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in segs))
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final])
    print("Rendered", final)

    desc = (f"{plan['description']}\n\nChapters:\n" + "\n".join(chapters) +
            "\n\nSources:\n" + "\n".join(w["link"] for w in items) +
            "\n\nStock footage: Pexels")
    if not dry:
        upload(final, plan["title"], desc, plan["tags"], cfg, cfg["weekly"]["privacy"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["short", "weekly"])
    ap.add_argument("--dry-run", action="store_true", help="render only, do not upload")
    a = ap.parse_args()
    cfg = load_cfg()
    (cmd_short if a.mode == "short" else cmd_weekly)(cfg, a.dry_run)


if __name__ == "__main__":
    main()
