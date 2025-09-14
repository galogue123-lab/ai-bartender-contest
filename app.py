import contextlib
from dotenv import load_dotenv
load_dotenv()
import os
import subprocess
import tempfile
import uuid
import json
import re
from pathlib import Path
from flask import Flask, request, render_template, send_file, jsonify, abort
import requests
import google.generativeai as genai
from PIL import Image, ImageDraw, ImageFont
from card_renderer import render_recipe_card

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Use Secret Manager when deploying.")
genai.configure(api_key=API_KEY)

app = Flask(__name__)
import logging
app.logger.setLevel(logging.INFO)

PROMPT_TEMPLATE = """
You are an AI mixology teacher.
Create a short storyboard for a 60-second VERTICAL TikTok lesson about making
the cocktail "{name}" ({spec}).

Return ONLY JSON in this schema (no extra text):
{{
  "cocktail_name": "string",
  "language": "{language}",
  "steps": [
    {{"step_number": 1, "narration": "spoken line under 9s", "caption": "<= 60 chars"}}
  ],
  "closing_line": "short sign-off"
}}
"""

def strip_code_fences(t: str) -> str:
    t = t.strip()
    if t.startswith("```"):
        t = t.strip("`")
        lines = []
        skip = True
        for line in t.splitlines():
            if skip and line.strip().lower().startswith("json"):
                skip = False
                continue
            lines.append(line)
        t = "\n".join(lines).strip()
    return t

def srt_ts(sec: float) -> str:
    if sec < 0: sec = 0
    m = int(sec // 60); s = int(sec % 60); ms = int(round((sec - int(sec))*1000))
    return f"{m:02d}:{s:02d},{ms:03d}"

def build_srt(steps, closing, total=60.0) -> str:
    n = len(steps) + 1
    slot = total / n if n else total
    t = 0.0
    idx = 1
    out = []
    for st in steps:
        start, end = t, min(total, t + slot)
        caption = (st.get("caption") or st.get("narration") or "").strip()
        out.append(f"{idx}\n00:{srt_ts(start)} --> 00:{srt_ts(end)}\n{caption}\n")
        t = end; idx += 1
    out.append(f"{idx}\n00:{srt_ts(t)} --> 00:{srt_ts(total)}\n{closing.strip()}\n")
    return "\n".join(out)

def count_srt_cues(srt_text: str) -> int:
    return sum(1 for line in srt_text.splitlines() if re.fullmatch(r"\s*\d+\s*", line))

def generate_placeholders(n: int, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = 1080, 1920
    BG = (245, 245, 245); FG = (20, 20, 20)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 120)
        small = ImageFont.truetype("DejaVuSans.ttf", 48)
    except:
        font = ImageFont.load_default(); small = ImageFont.load_default()
    paths = []
    for i in range(1, n+1):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        title = f"Step {i}"
        tb = d.textbbox((0,0), title, font=font)
        tw, th = tb[2]-tb[0], tb[3]-tb[1]
        d.text(((W-tw)//2, H//3 - th//2), title, fill=FG, font=font)
        footer = "AI Bartender Teacher"
        fb = d.textbbox((0,0), footer, font=small)
        fw, fh = fb[2]-fb[0], fb[3]-fb[1]
        d.text(((W-fw)//2, H-140), footer, fill=(90,90,90), font=small)
        p = out_dir / f"step_{i}.jpg"
        img.save(p, quality=92)
        paths.append(p)
    return paths

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

@app.route("/api/storyboard", methods=["POST"])
def storyboard():
    data = request.get_json(force=True)
    name = data.get("name", "Forest Whisperer")
    spec = data.get("spec", "vodka 1.5 oz, maraschino 0.5 oz, cranberry 1 oz, lemon 0.5 oz; shake hard; fine strain; coupe; lemon twist")
    language = data.get("language", "English")
    prompt = PROMPT_TEMPLATE.format(name=name, spec=spec, language=language)
    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(prompt)
    txt = strip_code_fences(resp.text)
    sb = json.loads(txt)
    srt = build_srt(sb["steps"], sb.get("closing_line", "Cheers!"), total=60.0)
    narration = "\n".join([s.get("narration","") for s in sb["steps"]] + [sb.get("closing_line","Cheers!")])
    return jsonify({"storyboard": sb, "srt": srt, "narration_script": narration})

@app.route("/api/tts", methods=["POST"])
def tts():
    """
    Generate WAV from text.
    - If `voice` starts with `elevenlabs:VOICE_ID`, call ElevenLabs API directly
      using the ELEVENLABS_API_KEY secret.
    - Otherwise, fall back to the existing command-template path (edge-tts, etc).
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = (data.get("voice") or "").strip()
    if not text:
        return ("Missing 'text' in request body.", 400)

    tmpdir = Path(tempfile.gettempdir())
    outp = tmpdir / f"tts_{uuid.uuid4().hex}.wav"

    # ---- Path A: ElevenLabs ----
    if voice.startswith("elevenlabs:"):
        voice_id = voice.split(":", 1)[1].strip()
        api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not api_key:
            app.logger.error("ELEVENLABS_API_KEY is not set")
            return ("TTS failed: ELEVENLABS_API_KEY not configured.", 500)
        try:
            import requests  # already in requirements
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "text": text,
                # model_id is optional for many voices; include if you’ve set one:
                # "model_id": "eleven_monolingual_v1"
            }
            r = requests.post(url, headers=headers, json=payload, stream=True, timeout=60)
            if r.status_code != 200:
                # Try to log JSON error payload if any
                err = None
                try:
                    err = r.json()
                except Exception:
                    err = r.text[:500]
                app.logger.error("ElevenLabs TTS failed: %s %s", r.status_code, err)
                return ("TTS failed (ElevenLabs). See server logs for details.", 502)
            with open(outp, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if not outp.exists() or outp.stat().st_size == 0:
                app.logger.error("ElevenLabs returned empty audio stream")
                return ("TTS failed (empty audio).", 502)
            return send_file(outp, mimetype="audio/wav", as_attachment=False)
        except Exception as e:
            app.logger.exception("ElevenLabs request error: %s", e)
            return ("TTS failed (exception). See logs.", 500)

    # ---- Path B: command-template fallback (edge-tts, etc) ----
    inp = tmpdir / f"tts_{uuid.uuid4().hex}.txt"
    try:
        inp.write_text(text, encoding="utf-8")
        tmpl = os.environ.get(
            "TTS_CMD_TEMPLATE",
            "edge-tts --voice '{voice}' --text \"{text}\" --write-media {out}"
        )
        fallback_voice = voice or "en-US-AriaNeural"
        cmd = tmpl.format(**{"in": str(inp), "out": str(outp), "voice": fallback_voice, "text": text})
        subprocess.run(cmd, shell=True, check=True)
        if not outp.exists() or outp.stat().st_size == 0:
            app.logger.error("TTS fallback produced no audio.")
            raise RuntimeError("TTS produced no audio.")
        app.logger.info(f"/api/tts finish: fallback voice={fallback_voice}, audio_size={outp.stat().st_size}")
        return send_file(outp, mimetype="audio/wav", as_attachment=False)
    except subprocess.CalledProcessError as e:
        app.logger.exception(f"TTS command failed: {e}")
        return ("TTS failed. Check server logs or TTS_CMD_TEMPLATE.", 500)
    except Exception as e:
        app.logger.exception(f"TTS (fallback) failed: {e}")
        return ("TTS failed. Check server logs or TTS_CMD_TEMPLATE.", 500)
    finally:
        with contextlib.suppress(Exception):
            inp.unlink(missing_ok=True)

        # End of /api/tts

@app.route("/api/compose", methods=["POST"])
def compose():
    """
    Compose MP4 from uploaded images + SRT; if images absent, auto-generate placeholders.
    Also inserts a Whisky & Ember recipe card as the first frame.
    """
    work = Path(tempfile.mkdtemp(prefix="bartender_"))
    assets = work / "assets"; assets.mkdir(parents=True, exist_ok=True)
    tmp = work / "tmp"; tmp.mkdir(parents=True, exist_ok=True)
    captions = work / "captions"; captions.mkdir(parents=True, exist_ok=True)

    files = []
    for f in request.files.getlist("files"):
        if not f.filename: continue
        dst = assets / f.filename
        f.save(dst)
        files.append(dst)

    srt_text = request.form.get("srt", "")
    srt_path = captions / "lesson.srt"
    srt_path.write_text(srt_text, encoding="utf-8")

    if not files:
        n = count_srt_cues(srt_text) or 6
        files = generate_placeholders(n, assets)

    title = request.form.get("title", "Forest Whisperer")
    ingredients = ["1.5 oz vodka","0.5 oz maraschino","1 oz cranberry","0.5 oz lemon"]
    method      = ["Shake hard with ice","Fine strain to coupe","Garnish: lemon twist"]
    card_png = assets / "recipe_card.png"
    render_recipe_card(title, ingredients, method, card_png)
    files = [card_png] + files

    audio_file = request.files.get("audio", None)
    audio_path = None
    if audio_file and audio_file.filename:
        audio_path = work / "audio.wav"
        audio_file.save(audio_path)

    per = 60.0 / max(1, len(files))
    final_mp4 = work / "lesson_final.mp4"
    cmd = ["ffmpeg", "-y"]
    for i, img in enumerate(files):
        cmd += ["-loop", "1", "-t", str(per), "-i", str(img)]
    if audio_path:
        cmd += ["-i",str(audio_path),"-c:a","aac","-b:a","192k","-shortest"]
    cmd += ["-filter_complex", f"[0:v][1:v][2:v]concat=n={len(files)}:v=1:a=0[outv]", "-map", "[outv]", str(final_mp4)]
    subprocess.run(cmd, check=True)

    return send_file(str(final_mp4), mimetype="video/mp4", as_attachment=True, download_name="lesson_final.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
