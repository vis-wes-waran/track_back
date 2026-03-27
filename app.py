import os
import re
import base64
import json
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from groq import Groq

app = Flask(__name__)
CORS(app)

client = Groq(api_key=os.getenv("GROQ_API_KEY", "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"))
DATABASE = "junkstop.db"

# ════════════════════════════════════════════════════════
#  ROBUST JSON EXTRACTOR  — handles every model quirk
# ════════════════════════════════════════════════════════
def extract_json(raw: str) -> dict:
    """
    Multi-strategy JSON extractor.
    Strategy 1 – direct parse (model was well-behaved)
    Strategy 2 – strip markdown fences then parse
    Strategy 3 – brace-counter: walk char-by-char to find the
                  outermost complete { … } block
    Strategy 4 – regex: grab everything between the FIRST { and
                  the LAST } (handles trailing garbage)
    Strategy 5 – fix common single-quote / trailing-comma issues
                  then retry each strategy above
    """
    if not raw:
        raise ValueError("Empty response from model")

    def try_parse(s: str) -> dict:
        return json.loads(s)

    def brace_extract(s: str) -> str:
        depth = 0
        start = None
        for i, ch in enumerate(s):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    return s[start:i + 1]
        return s

    def fence_strip(s: str) -> str:
        s = re.sub(r'```[a-zA-Z]*', '', s)
        return s.strip()

    def light_fix(s: str) -> str:
        # trailing commas before } or ]
        s = re.sub(r',\s*([}\]])', r'\1', s)
        # single-quoted keys/values → double-quoted
        s = re.sub(r"'([^']*)'", r'"\1"', s)
        # unquoted keys
        s = re.sub(r'(\{|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
        return s

    attempts = [
        raw,
        fence_strip(raw),
        brace_extract(raw),
        brace_extract(fence_strip(raw)),
    ]

    for attempt in attempts:
        try:
            return try_parse(attempt)
        except Exception:
            pass

    # Try with light fixes
    for attempt in attempts:
        try:
            return try_parse(light_fix(attempt))
        except Exception:
            pass

    # Last resort: find first { … last } by regex
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        chunk = m.group(0)
        for s in [chunk, light_fix(chunk)]:
            try:
                return try_parse(s)
            except Exception:
                pass

    raise ValueError(f"Could not parse JSON. Raw preview: {raw[:300]}")


# ════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS food_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        TEXT    NOT NULL DEFAULT 'default',
                food_name      TEXT    NOT NULL,
                emoji          TEXT    DEFAULT '🍽️',
                junk_score     INTEGER NOT NULL,
                category       TEXT    NOT NULL,
                classification TEXT    NOT NULL,
                calories_min   INTEGER DEFAULT 0,
                calories_max   INTEGER DEFAULT 0,
                protein_g      REAL    DEFAULT 0,
                carbs_g        REAL    DEFAULT 0,
                fat_g          REAL    DEFAULT 0,
                fiber_g        REAL    DEFAULT 0,
                sugar_g        REAL    DEFAULT 0,
                sodium_mg      REAL    DEFAULT 0,
                ingredients    TEXT    DEFAULT '[]',
                health_effects TEXT    DEFAULT '[]',
                alternatives   TEXT    DEFAULT '[]',
                summary        TEXT    DEFAULT '',
                motivation     TEXT    DEFAULT '',
                logged_at      TEXT    NOT NULL,
                meal_type      TEXT    DEFAULT 'snack',
                notes          TEXT    DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS daily_goals (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        TEXT NOT NULL DEFAULT 'default',
                goal_date      TEXT NOT NULL,
                calorie_target INTEGER DEFAULT 2000,
                junk_limit     INTEGER DEFAULT 1,
                UNIQUE(user_id, goal_date)
            );
        """)
        db.commit()


# ════════════════════════════════════════════════════════
#  PROMPTS
# ════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are a food nutrition analyst AI.
CRITICAL RULE: Your ENTIRE response must be ONE raw JSON object.
- Do NOT write any text before or after the JSON.
- Do NOT use markdown code fences (no backticks).
- Do NOT add explanations or apologies.
- Start your response with { and end it with }
- Nothing else."""

def make_analysis_prompt(food_desc: str) -> str:
    return f"""Analyze this food: {food_desc}

Return this exact JSON structure and nothing else:
{{
  "foodName": "name of the food",
  "emoji": "one emoji",
  "junkScore": 0,
  "category": "Healthy",
  "classification": "GOOD",
  "classificationReason": "one sentence",
  "summary": "two sentences about nutrition",
  "calories": {{
    "min": 200,
    "max": 300,
    "serving_size": "1 serving (100g)"
  }},
  "macros": {{
    "protein_g": 10,
    "carbs_g": 30,
    "fat_g": 5,
    "fiber_g": 3,
    "sugar_g": 5,
    "sodium_mg": 200
  }},
  "ingredients": [
    {{"name": "ingredient", "harm": "why harmful in 10 words", "severity": "high"}}
  ],
  "health_effects": ["effect 1", "effect 2"],
  "alternatives": [
    {{"name": "food", "emoji": "🥗", "why": "reason", "calories": "150 kcal"}}
  ],
  "motivation": "A strong 25-word psychological message to motivate healthy eating"
}}

RULES:
- junkScore is integer 0-100. If score < 45 set classification=GOOD, if >= 45 set classification=BAD
- ingredients: list only harmful ones, max 4. If healthy food, use empty array []
- health_effects: max 3 items
- alternatives: max 2 items
- motivation: if BAD food, write a STRONG psychological warning about long-term damage. If GOOD food, write enthusiastic praise.
- All numbers must be realistic for a typical serving
- RESPOND WITH ONLY THE JSON. NO OTHER TEXT."""


def make_motivation_prompt(food_name: str, junk_score: int, is_repeat: bool, repeat_count: int, goal: str) -> str:
    if is_repeat and repeat_count > 0:
        return f"""You are a blunt health psychologist. The user has eaten "{food_name}" (junk score {junk_score}/100) {repeat_count + 1} times this week despite knowing it is harmful.

Write a powerful 3-sentence psychological intervention message that:
1. Names the EXACT health damage happening RIGHT NOW in their body from repeat consumption
2. Uses loss aversion — what they are LOSING (years of life, energy, health)  
3. Ends with an empowering action they can take RIGHT NOW

Be direct, specific, and emotionally impactful. No generic advice. Mention the food name.
Return ONLY the message text."""
    elif junk_score >= 70:
        return f"""You are a compassionate but firm health coach. The user just analyzed "{food_name}" (junk score {junk_score}/100) — highly processed junk food. Their goal: {goal}.

Write a powerful 2-sentence motivational message that:
1. Describes one specific biological harm of eating this food (be scientific and specific)
2. Gives them a moment of clarity — "every time you skip this, you are choosing X instead"

Be warm but honest. Make it memorable. Return ONLY the message text."""
    else:
        return f"""The user just analyzed "{food_name}" (junk score {junk_score}/100). Their goal: {goal}.

Write a 2-sentence encouraging message that celebrates their healthy choice and motivates them to keep going.
Be specific about the benefits of this food. Return ONLY the message text."""


# ════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════

@app.route("/api/health", methods=["GET"])
def health():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM food_log").fetchone()["c"]
    return jsonify({"status": "ok", "model": "meta-llama/llama-4-scout-17b-16e-instruct", "total_logged": count})


@app.route("/api/analyze/text", methods=["POST"])
def analyze_text():
    data      = request.get_json(silent=True) or {}
    food_name = data.get("food", "").strip()
    if not food_name:
        return jsonify({"error": "No food name provided"}), 400

    raw = ""
    try:
        comp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": make_analysis_prompt(f'"{food_name}"')},
            ],
            temperature=0.2,
            max_tokens=1400,
            top_p=1,
            stream=False,
        )
        raw    = comp.choices[0].message.content
        result = extract_json(raw)

        # Inject enhanced motivation separately for reliability
        motiv = get_motivation_text(
            result.get("foodName", food_name),
            result.get("junkScore", 50),
            False, 0,
            data.get("goal", "reduce junk food")
        )
        result["motivation"] = motiv
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "raw_preview": raw[:400] if raw else ""}), 500


@app.route("/api/analyze/image", methods=["POST"])
def analyze_image():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    img_file  = request.files["image"]
    img_bytes = img_file.read()
    mime_type = img_file.content_type or "image/jpeg"
    b64       = base64.b64encode(img_bytes).decode("utf-8")

    raw = ""
    try:
        comp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    {"type": "text",
                     "text": "Identify the food in this image, then:\n\n" + make_analysis_prompt("the food shown in the image")},
                ]},
            ],
            temperature=0.2,
            max_tokens=1400,
            top_p=1,
            stream=False,
        )
        raw    = comp.choices[0].message.content
        result = extract_json(raw)

        motiv = get_motivation_text(
            result.get("foodName", "this food"),
            result.get("junkScore", 50),
            False, 0, "reduce junk food"
        )
        result["motivation"] = motiv
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "raw_preview": raw[:400] if raw else ""}), 500


def get_motivation_text(food_name, junk_score, is_repeat, repeat_count, goal):
    """Get AI motivation — called internally, never fails (returns fallback)."""
    try:
        prompt = make_motivation_prompt(food_name, junk_score, is_repeat, repeat_count, goal)
        comp   = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=120,
            stream=False,
        )
        return comp.choices[0].message.content.strip().strip('"')
    except Exception:
        if junk_score >= 60:
            return f"Every time you choose {food_name}, you are trading 20 minutes of your future health for 5 minutes of taste. Your body deserves better — make the switch today."
        return f"Excellent choice! {food_name} is nourishing your body with real nutrients. Keep building this habit and your future self will thank you."


@app.route("/api/check-repeat", methods=["POST"])
def check_repeat():
    data           = request.get_json(silent=True) or {}
    user_id        = data.get("user_id", "default")
    food_name      = data.get("food_name", "").strip().lower()
    classification = data.get("classification", "GOOD")
    junk_score     = data.get("junk_score", 50)
    goal           = data.get("goal", "reduce junk food")

    if classification != "BAD":
        return jsonify({"repeat": False, "count": 0, "warning": None})

    db  = get_db()
    row = db.execute("""
        SELECT COUNT(*) as c FROM food_log
        WHERE user_id=?
          AND lower(food_name) LIKE ?
          AND classification='BAD'
          AND date(logged_at,'localtime') >= date('now','localtime','-7 days')
    """, (user_id, f"%{food_name[:10]}%")).fetchone()
    count = row["c"]

    if count == 0:
        return jsonify({"repeat": False, "count": 0, "warning": None})

    warning = get_motivation_text(
        data.get("food_name", food_name),
        junk_score, True, count, goal
    )
    return jsonify({"repeat": True, "count": count, "warning": warning})


@app.route("/api/log", methods=["POST"])
def log_food():
    data = request.get_json(silent=True) or {}
    for f in ["food_name", "junk_score", "category", "classification"]:
        if f not in data:
            return jsonify({"error": f"Missing: {f}"}), 400

    cal    = data.get("calories", {})
    macros = data.get("macros", {})
    now    = datetime.now().astimezone().isoformat()
    db     = get_db()
    cur    = db.execute("""
        INSERT INTO food_log (
            user_id, food_name, emoji, junk_score, category, classification,
            calories_min, calories_max, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg,
            ingredients, health_effects, alternatives, summary, motivation, logged_at, meal_type, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("user_id", "default"),
        data["food_name"], data.get("emoji", "🍽️"),
        int(data["junk_score"]), data["category"], data["classification"],
        int(cal.get("min", 0)), int(cal.get("max", 0)),
        float(macros.get("protein_g", 0)), float(macros.get("carbs_g", 0)),
        float(macros.get("fat_g", 0)),    float(macros.get("fiber_g", 0)),
        float(macros.get("sugar_g", 0)),  float(macros.get("sodium_mg", 0)),
        json.dumps(data.get("ingredients", [])),
        json.dumps(data.get("health_effects", [])),
        json.dumps(data.get("alternatives", [])),
        data.get("summary", ""), data.get("motivation", ""),
        now, data.get("meal_type", "snack"), data.get("notes", "")
    ))
    db.commit()
    return jsonify({"success": True, "id": cur.lastrowid, "logged_at": now}), 201


@app.route("/api/log/today", methods=["GET"])
def get_today_log():
    user_id = request.args.get("user_id", "default")
    today   = date.today().isoformat()
    db      = get_db()
    rows    = db.execute("""
        SELECT * FROM food_log
        WHERE user_id=? AND date(logged_at,'localtime')=?
        ORDER BY logged_at DESC
    """, (user_id, today)).fetchall()

    entries = []
    cal_min = cal_max = protein = carbs = fat = fiber = junk_count = good_count = 0
    for row in rows:
        e = dict(row)
        e["ingredients"]    = json.loads(e.get("ingredients") or "[]")
        e["health_effects"] = json.loads(e.get("health_effects") or "[]")
        e["alternatives"]   = json.loads(e.get("alternatives") or "[]")
        entries.append(e)
        cal_min += e["calories_min"]; cal_max += e["calories_max"]
        protein += e["protein_g"];    carbs   += e["carbs_g"]
        fat     += e["fat_g"];        fiber   += e["fiber_g"]
        if e["classification"] == "BAD": junk_count += 1
        else: good_count += 1

    return jsonify({
        "date": today, "entries": entries,
        "totals": {
            "calories_min": cal_min, "calories_max": cal_max,
            "calories_avg": (cal_min + cal_max) // 2 if entries else 0,
            "protein_g": round(protein, 1), "carbs_g": round(carbs, 1),
            "fat_g": round(fat, 1), "fiber_g": round(fiber, 1),
            "meal_count": len(entries), "good_count": good_count, "junk_count": junk_count,
        }
    })


@app.route("/api/log/history", methods=["GET"])
def get_history():
    user_id = request.args.get("user_id", "default")
    days    = int(request.args.get("days", 7))
    db      = get_db()
    rows    = db.execute("""
        SELECT date(logged_at,'localtime') as day,
               COUNT(*) as meals,
               SUM(calories_min) as cal_min, SUM(calories_max) as cal_max,
               AVG(junk_score) as avg_junk,
               SUM(CASE WHEN classification='BAD'  THEN 1 ELSE 0 END) as junk_meals,
               SUM(CASE WHEN classification='GOOD' THEN 1 ELSE 0 END) as good_meals,
               SUM(protein_g) as protein, SUM(carbs_g) as carbs, SUM(fat_g) as fat
        FROM food_log
        WHERE user_id=?
          AND date(logged_at,'localtime') >= date('now','localtime',? || ' days')
        GROUP BY day ORDER BY day DESC
    """, (user_id, f"-{days}")).fetchall()
    return jsonify({"days": days, "history": [dict(r) for r in rows]})


@app.route("/api/log/all", methods=["GET"])
def get_all_log():
    user_id = request.args.get("user_id", "default")
    limit   = int(request.args.get("limit", 50))
    offset  = int(request.args.get("offset", 0))
    db      = get_db()
    rows    = db.execute("""
        SELECT * FROM food_log WHERE user_id=?
        ORDER BY logged_at DESC LIMIT ? OFFSET ?
    """, (user_id, limit, offset)).fetchall()
    total   = db.execute("SELECT COUNT(*) as c FROM food_log WHERE user_id=?", (user_id,)).fetchone()["c"]
    entries = []
    for row in rows:
        e = dict(row)
        e["ingredients"]    = json.loads(e.get("ingredients") or "[]")
        e["health_effects"] = json.loads(e.get("health_effects") or "[]")
        e["alternatives"]   = json.loads(e.get("alternatives") or "[]")
        entries.append(e)
    return jsonify({"entries": entries, "total": total})


@app.route("/api/log/<int:entry_id>", methods=["DELETE"])
def delete_log(entry_id):
    db = get_db()
    db.execute("DELETE FROM food_log WHERE id=?", (entry_id,))
    db.commit()
    return jsonify({"success": True})


@app.route("/api/stats/summary", methods=["GET"])
def get_stats():
    user_id   = request.args.get("user_id", "default")
    db        = get_db()
    total     = db.execute("SELECT COUNT(*) as c FROM food_log WHERE user_id=?", (user_id,)).fetchone()["c"]
    today_row = db.execute("""
        SELECT COUNT(*) as c, SUM(calories_min+calories_max)/2 as cal
        FROM food_log WHERE user_id=? AND date(logged_at,'localtime')=date('now','localtime')
    """, (user_id,)).fetchone()
    good_total = db.execute("SELECT COUNT(*) as c FROM food_log WHERE user_id=? AND classification='GOOD'", (user_id,)).fetchone()["c"]
    bad_total  = db.execute("SELECT COUNT(*) as c FROM food_log WHERE user_id=? AND classification='BAD'",  (user_id,)).fetchone()["c"]
    avg_score  = db.execute("SELECT AVG(junk_score) as a FROM food_log WHERE user_id=?", (user_id,)).fetchone()["a"] or 0
    week_cal   = db.execute("""
        SELECT SUM(calories_min+calories_max)/2 as cal FROM food_log
        WHERE user_id=? AND date(logged_at,'localtime')>=date('now','localtime','-7 days')
    """, (user_id,)).fetchone()["cal"] or 0
    return jsonify({
        "total_logged": total, "good_count": good_total, "bad_count": bad_total,
        "avg_junk_score": round(avg_score, 1),
        "today_meals": today_row["c"], "today_calories": int(today_row["cal"] or 0),
        "week_calories": int(week_cal),
        "health_score": max(0, round(100 - avg_score)),
    })


@app.route("/api/bad-habits", methods=["GET"])
def bad_habits():
    user_id = request.args.get("user_id", "default")
    db      = get_db()

    week_bad = db.execute("""
        SELECT food_name, emoji, AVG(junk_score) as avg_score,
               COUNT(*) as times, MAX(logged_at) as last_eaten
        FROM food_log
        WHERE user_id=? AND classification='BAD'
          AND date(logged_at,'localtime') >= date('now','localtime','-7 days')
        GROUP BY lower(food_name)
        ORDER BY times DESC, avg_score DESC
    """, (user_id,)).fetchall()

    week_good = db.execute("""
        SELECT food_name, emoji, COUNT(*) as times, MAX(logged_at) as last_eaten
        FROM food_log
        WHERE user_id=? AND classification='GOOD'
          AND date(logged_at,'localtime') >= date('now','localtime','-7 days')
        GROUP BY lower(food_name)
        ORDER BY times DESC LIMIT 5
    """, (user_id,)).fetchall()

    last_week_bad = db.execute("""
        SELECT DISTINCT lower(food_name) as fn FROM food_log
        WHERE user_id=? AND classification='BAD'
          AND date(logged_at,'localtime') >= date('now','localtime','-14 days')
          AND date(logged_at,'localtime') <  date('now','localtime','-7 days')
    """, (user_id,)).fetchall()
    this_week_names = {r["food_name"].lower() for r in week_bad}
    avoided = [r["fn"] for r in last_week_bad if r["fn"] not in this_week_names]

    clean_days = 0
    for i in range(60):
        day     = (date.today() - timedelta(days=i)).isoformat()
        bad_row = db.execute("""
            SELECT COUNT(*) as c FROM food_log
            WHERE user_id=? AND classification='BAD'
              AND date(logged_at,'localtime')=?
        """, (user_id, day)).fetchone()
        if bad_row["c"] > 0:
            break
        logged = db.execute("""
            SELECT COUNT(*) as c FROM food_log
            WHERE user_id=? AND date(logged_at,'localtime')=?
        """, (user_id, day)).fetchone()
        if logged["c"] > 0:
            clean_days += 1

    return jsonify({
        "repeat_bad_foods":  [dict(r) for r in week_bad],
        "top_good_foods":    [dict(r) for r in week_good],
        "avoided_this_week": avoided,
        "clean_streak_days": clean_days,
    })


@app.route("/api/motivate", methods=["POST"])
def get_motivation():
    data       = request.get_json(silent=True) or {}
    junk_count = data.get("junk_count", 0)
    goal       = data.get("goal", "reduce junk food")
    prompt = (
        f'User goal: {goal}. Junk meals this week: {junk_count}. '
        f'Write ONE powerful psychological motivational message (25-35 words) about healthy eating. '
        f'Make it specific, emotional, and memorable. Return ONLY the message.'
    )
    try:
        comp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0, max_tokens=100, stream=False,
        )
        return jsonify({"motivation": comp.choices[0].message.content.strip().strip('"')})
    except Exception as e:
        return jsonify({"motivation": "Every healthy meal is a vote for the person you are becoming. Your body is listening — make the choice count."}), 200


@app.route("/api/insights", methods=["POST"])
def get_insights():
    data = request.get_json(silent=True) or {}
    log  = data.get("log", [])
    goal = data.get("goal", "reduce junk food")
    if not log:
        return jsonify({"insights": ["Start logging meals to get personalized AI insights!"]})

    food_summary = ", ".join([
        f"{e.get('food_name','?')} ({e.get('classification','?')}, {e.get('calories_min',0)}-{e.get('calories_max',0)} kcal)"
        for e in log[-10:]
    ])
    prompt = (
        f'User goal: {goal}. Recent meals: {food_summary}. '
        f'Generate exactly 3 specific actionable health insights. '
        f'Return ONLY a JSON array like: ["insight 1", "insight 2", "insight 3"]. No other text.'
    )
    raw = ""
    try:
        comp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8, max_tokens=400, stream=False,
        )
        raw = comp.choices[0].message.content.strip()
        # extract array
        a_start = raw.find("["); a_end = raw.rfind("]")
        if a_start != -1 and a_end != -1:
            insights = json.loads(raw[a_start:a_end+1])
        else:
            insights = json.loads(raw)
        return jsonify({"insights": insights})
    except Exception:
        return jsonify({"insights": ["Analyze more foods to unlock personalized AI insights."]})


@app.route("/api/daily-summary", methods=["POST"])
def daily_summary():
    data       = request.get_json(silent=True) or {}
    entries    = data.get("entries", [])
    totals     = data.get("totals", {})
    goal       = data.get("goal", "reduce junk food")
    cal_target = int(data.get("cal_target", 2000))

    if not entries:
        return jsonify({
            "summary": "No meals logged today yet. Start by analyzing your first food!",
            "rating": "N/A", "rating_emoji": "🍽️",
            "calorie_verdict": "No calorie data yet.",
            "good_choices": [], "bad_choices": [],
            "recommendations": ["Log your meals throughout the day to get a personalized summary."],
            "tomorrow_plan": "Start fresh tomorrow — every meal is a new opportunity!"
        })

    good_foods = [e for e in entries if e.get("classification") == "GOOD"]
    bad_foods  = [e for e in entries if e.get("classification") == "BAD"]
    total_cal  = totals.get("calories_avg", 0)
    cal_diff   = total_cal - cal_target
    cal_note   = f"over by {abs(cal_diff)}" if cal_diff > 0 else f"under by {abs(cal_diff)}"
    meal_list  = "\n".join([
        f"- {e.get('food_name')} ({e.get('classification')}, score {e.get('junk_score')}, "
        f"~{round((e.get('calories_min',0)+e.get('calories_max',0))/2)} kcal, {e.get('meal_type')})"
        for e in entries
    ])

    prompt = (
        f"You are a warm but honest personal nutrition coach.\n\n"
        f"User goal: {goal}\n"
        f"Calorie target: {cal_target} kcal | Today: {total_cal} kcal ({cal_note})\n"
        f"Macros: Protein {round(totals.get('protein_g',0),1)}g | Carbs {round(totals.get('carbs_g',0),1)}g | Fat {round(totals.get('fat_g',0),1)}g\n"
        f"Good foods: {len(good_foods)} | Bad foods: {len(bad_foods)}\n\n"
        f"Today meals:\n{meal_list}\n\n"
        f"Return ONLY this JSON object with no other text:\n"
        f'{{"summary":"3-4 warm honest sentences mentioning actual food names",'
        f'"rating":"Excellent or Good or Fair or Poor",'
        f'"rating_emoji":"one emoji",'
        f'"calorie_verdict":"1-2 sentences about calories vs target with exact numbers",'
        f'"good_choices":["specific good food + why - 1 sentence"],'
        f'"bad_choices":["specific bad food + one concrete harm - 1 sentence"],'
        f'"recommendations":["actionable swap for tomorrow","nutrient tip based on today macros","specific habit tip"],'
        f'"tomorrow_plan":"1-2 motivating sentences for tomorrow"}}'
    )

    raw = ""
    try:
        comp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85, max_tokens=900, stream=False,
        )
        raw    = comp.choices[0].message.content
        result = extract_json(raw)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── BOOT ──
init_db()
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)