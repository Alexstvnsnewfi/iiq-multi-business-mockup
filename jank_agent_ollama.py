"""
Jank computer-use agent, Ollama edition.

Setup:
    ollama pull qwen2.5vl:7b          # any vision model works; this one is trained for UI grounding
    pip install pyautogui pillow requests
    python jank_agent_ollama.py "open notepad and type hello world"

Differences from the Claude version:
  - No tool calling. The model is told to reply with one JSON object per turn.
  - Stateless steps: each request has only the NEWEST screenshot plus a text log of
    previous actions (small local models have small contexts and get confused by many images).
  - Saves debug_step_N.png with a red circle where it's about to click, so you can see
    whether coordinates are landing correctly.
"""
import base64
import io
import json
import re
import sys
import time
import webbrowser

import pyautogui
import requests
from PIL import ImageChops, ImageDraw, ImageGrab

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5vl:7b"
MAX_STEPS = 20
CONFIRM = False         # True = type y/N in the terminal (steals focus, see notes)
ACTION_DELAY = 4        # seconds to wait before each action when CONFIRM is False
MINIMIZE_MAIN_MONITOR_FIRST = True  # minimize windows on the PRIMARY monitor only (Windows)
TARGET_W = 1008         # smaller = faster on local hardware; raise if text is unreadable
SNAP = 28               # Qwen-VL works in 28px blocks; sending w/h that are multiples of this
                        # avoids the model's internal resize, a common cause of off-target clicks
HISTORY_LINES = 8       # how many past actions to repeat back to the model
SAVE_DEBUG = True
COORD_MULT = 0.833      # multiplies the model's x/y. Your debug images show the model aiming ~1.2x too far
                        # from the top-left, so 1/1.2 = 0.833. Run --calibrate to get a measured value.

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

pyautogui.FAILSAFE = True   # slam mouse to top-left corner to abort
pyautogui.PAUSE = 0.3

SYSTEM = """You control a computer by looking at screenshots.
Reply with EXACTLY ONE JSON object and nothing else, in this form:
{"reason": "<one short sentence>", "action": "<action>", "x": <int>, "y": <int>, "text": "<string>", "amount": <int>}

Actions:
  click, double_click, right_click  -> need x and y (pixels in the screenshot, origin top-left)
  type    -> text is the string to type into the focused field
  key     -> text is a key or combo such as "enter" or "ctrl+s"
  scroll  -> amount is scroll clicks (positive = up, negative = down)
  open_url -> text is a full URL; opens it in the default web browser (best way to open a website)
  wait    -> wait 2 seconds
  done    -> goal is finished; put a short summary in text

Only include the fields the action needs. Take ONE step at a time.
Aim at the CENTER of the icon or button itself, not its text label.
On the Windows desktop, icons only open with double_click; a single click just selects them.
Look at the screenshot to check whether your last action worked before continuing."""


def minimize_primary_monitor_windows():
    """Minimize every normal window on the primary monitor. Other monitors are left alone,
    so this terminal can stay visible on monitor 2. Windows only."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    for name, args, res in [
        ("MonitorFromPoint", [wintypes.POINT, wintypes.DWORD], wintypes.HMONITOR),
        ("MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], wintypes.HMONITOR),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("IsIconic", [wintypes.HWND], wintypes.BOOL),
        ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
        ("GetWindowLongW", [wintypes.HWND, ctypes.c_int], ctypes.c_long),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("GetShellWindow", [], wintypes.HWND),
        ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
    ]:
        f = getattr(u, name)
        f.argtypes, f.restype = args, res

    primary = u.MonitorFromPoint(wintypes.POINT(0, 0), 1)       # MONITOR_DEFAULTTOPRIMARY
    skip = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd) or u.GetWindowTextLengthW(hwnd) == 0:
            return True
        if u.GetWindowLongW(hwnd, -20) & 0x80:                   # WS_EX_TOOLWINDOW
            return True
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, buf, 256)
        if buf.value in skip:
            return True
        if u.MonitorFromWindow(hwnd, 2) == primary:              # MONITOR_DEFAULTTONEAREST
            u.ShowWindow(hwnd, 6)                                # SW_MINIMIZE
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    # Move keyboard focus off this terminal so typed text goes to the main monitor.
    u.SetForegroundWindow(u.GetShellWindow())


def take_screenshot():
    img = ImageGrab.grab()   # primary monitor only (all_screens=False)
    w = max(SNAP, round(TARGET_W / SNAP) * SNAP)
    h = max(SNAP, round(img.height * w / img.width / SNAP) * SNAP)
    small = img.resize((w, h))
    sw, sh = pyautogui.size()                  # the coordinate space clicks actually use
    scale = (sw / w, sh / h)                   # separate x/y factors, since h is snapped too
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    return small, base64.b64encode(buf.getvalue()).decode(), scale


def ask_model(goal, history, b64, size):
    log = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history[-HISTORY_LINES:])) or "(none yet)"
    user = (f"Goal: {goal}\n"
            f"Screenshot size: {size[0]}x{size[1]} pixels.\n"
            f"Actions taken so far:\n{log}\n\n"
            "What is the single next action? JSON only.")
    r = requests.post(OLLAMA_URL, timeout=300, json={
        "model": MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user, "images": [b64]},
        ],
    })
    if not r.ok:
        sys.exit(f"Ollama error {r.status_code}: {r.text}\n"
                 f"(MODEL is set to {MODEL!r}. Run `ollama list` to see what's installed.)")
    return r.json()["message"]["content"]


def parse_action(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)   # fall back to the first {...} blob
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


def extract_numbers(v):
    """Pull every number out of a value: 512, "512", [512, 300], "(512, 300)" all work."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", str(v))]


def normalize_action(a):
    """Small models return coordinates in all sorts of shapes ("x": [512, 300],
    "coordinate": [512, 300], a 4-number bbox, strings...). Coerce to int x and y,
    or drop them if there aren't usable coordinates."""
    if isinstance(a, list) and a and isinstance(a[0], dict):
        a = a[0]
    if not isinstance(a, dict):
        return None
    raw = [a.get("x"), a.get("y")]
    if raw[0] is None and raw[1] is None:
        for key in ("coordinate", "coordinates", "point", "position", "pos", "xy", "bbox", "bbox_2d"):
            if key in a:
                raw = [a[key], None]
                break
    nums = [n for v in raw if v is not None for n in extract_numbers(v)]
    if len(nums) == 4:                                   # bounding box -> center
        nums = [(nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2]
    a = dict(a)
    if len(nums) >= 2:
        a["x"], a["y"] = round(nums[0]), round(nums[1])
    else:
        a.pop("x", None)
        a.pop("y", None)
    return a


def draw_debug(img, action, step):
    x, y = action.get("x"), action.get("y")
    if not SAVE_DEBUG or x is None or y is None:
        return
    shot = img.copy()
    d = ImageDraw.Draw(shot)
    d.ellipse((x - 12, y - 12, x + 12, y + 12), outline="red", width=3)
    shot.save(f"debug_step_{step}.png")


def execute(a, scale, size):
    kind = a.get("action")
    x, y = a.get("x"), a.get("y")
    needs_xy = kind in ("click", "double_click", "right_click")
    if needs_xy:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return "error: missing x/y"
        if not (0 <= x < size[0] and 0 <= y < size[1]):
            return f"error: ({x},{y}) is outside the {size[0]}x{size[1]} screenshot"
        real = (round(x * scale[0]), round(y * scale[1]))

    if kind == "click":
        pyautogui.click(*real)
    elif kind == "double_click":
        pyautogui.doubleClick(*real)
    elif kind == "right_click":
        pyautogui.rightClick(*real)
    elif kind == "type":
        pyautogui.write(str(a.get("text", "")), interval=0.02)
    elif kind == "key":
        pyautogui.hotkey(*str(a.get("text", "")).lower().split("+"))
    elif kind == "scroll":
        pyautogui.scroll(int((extract_numbers(a.get("amount", -3)) or [-3])[0]))
    elif kind == "open_url":
        webbrowser.open(str(a.get("text", "")))
        time.sleep(3)
    elif kind == "wait":
        time.sleep(2)
    else:
        return f"error: unknown action {kind!r}"
    time.sleep(1)
    return "ok"


def calibrate():
    """Test the model's click accuracy on a synthetic image with known targets.
    Separates 'the model aims badly' from 'the screen mapping is wrong'."""
    from PIL import Image

    w, h = take_screenshot()[0].size
    colors = {"red": (220, 30, 30), "green": (30, 160, 60), "blue": (40, 80, 220),
              "yellow": (240, 210, 30), "purple": (150, 50, 190), "orange": (240, 130, 20)}
    fracs = [(0.08, 0.12), (0.92, 0.12), (0.50, 0.50), (0.08, 0.88), (0.92, 0.88), (0.70, 0.30)]
    img = Image.new("RGB", (w, h), (235, 235, 235))
    d = ImageDraw.Draw(img)
    targets = []
    for (name, rgb), (fx, fy) in zip(colors.items(), fracs):
        cx, cy = round(fx * w), round(fy * h)
        d.rectangle((cx - 20, cy - 20, cx + 20, cy + 20), fill=rgb)
        targets.append((name, cx, cy))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    img.save("calibration_image.png")

    print(f"Calibration image is {w}x{h}. Asking the model to find 6 squares...\n")
    rows = []
    for name, tx, ty in targets:
        a = normalize_action(parse_action(
            ask_model(f"Click the center of the {name} square.", [], b64, img.size)))
        if not a or "x" not in a:
            print(f"{name:7} target ({tx:4},{ty:4})  -> no usable coordinates")
            continue
        print(f"{name:7} target ({tx:4},{ty:4})  model ({a['x']:4},{a['y']:4})  "
              f"ratio ({a['x'] / tx:.2f}, {a['y'] / ty:.2f})")
        rows.append((tx, ty, a["x"], a["y"]))
    if len(rows) < 3:
        print("\nNot enough usable answers to analyze.")
        return

    def fit(true, model):
        n = len(true)
        mt, mm = sum(true) / n, sum(model) / n
        slope = sum((t - mt) * (m - mm) for t, m in zip(true, model)) / sum((t - mt) ** 2 for t in true)
        return slope, mm - slope * mt

    sx, ox = fit([r[0] for r in rows], [r[2] for r in rows])
    sy, oy = fit([r[1] for r in rows], [r[3] for r in rows])
    print(f"\nFit: model_x = {sx:.3f} * true_x + {ox:.0f}    model_y = {sy:.3f} * true_y + {oy:.0f}")
    if all(0.97 <= v <= 1.03 for v in (sx, sy)) and abs(ox) < 12 and abs(oy) < 12:
        print("The model's coordinates are accurate on this test. If real clicks are still off,")
        print("the problem is the screen mapping or the model struggling with tiny real UI targets.")
    elif all(0.97 <= v <= 1.03 for v in (sx, sy)):
        print(f"Scale is fine but there is a constant offset of about ({ox:.0f}, {oy:.0f}) pixels.")
    else:
        avg = (sx + sy) / 2
        print(f"The model's coordinates are scaled by about {avg:.2f}x.")
        print(f"Set COORD_MULT = {1 / avg:.3f} at the top of the script.")
        if abs(sx - sy) > 0.05:
            print("(x and y scales differ noticeably, so one number won't fully fix it.)")


def screen_changed(before, after):
    """Heuristic: did the screen visibly change? Ignores the taskbar clock area."""
    diff = ImageChops.difference(before.convert("L"), after.convert("L"))
    w, h = diff.size
    diff.paste(0, (w - 140, h - 34, w, h))            # mask the clock/tray corner
    return sum(diff.histogram()[13:]) > 300           # >300 pixels differ noticeably


def run(goal):
    history = []
    prev_img = None
    last_acted = False
    for step in range(1, MAX_STEPS + 1):
        img, b64, scale = take_screenshot()
        if last_acted and prev_img is not None and history:
            if not screen_changed(prev_img, img):
                print("   (screen did not change after that action)")
                history[-1] += " [RESULT: the screen did not change, so that had no effect. Try something different.]"
            last_acted = False
        print(f"\n[{step}] thinking...")
        raw = ask_model(goal, history, b64, img.size)
        action = normalize_action(parse_action(raw))
        if not action:
            print("Couldn't parse model output:", raw[:300])
            history.append("(your last reply was not valid JSON; reply with one JSON object)")
            continue

        if "x" in action and "y" in action:
            print(f"[{step}] model coords: ({action['x']}, {action['y']}) on a {img.size[0]}x{img.size[1]} image")
            action["x"] = round(action["x"] * COORD_MULT)
            action["y"] = round(action["y"] * COORD_MULT)
        print(f"[{step}] {action}")
        if action.get("action") == "done":
            print("DONE:", action.get("text", ""))
            return

        # Bail out if the model is looping on the same action.
        sig = json.dumps({k: action.get(k) for k in ("action", "x", "y", "text", "amount")}, sort_keys=True)
        if history[-3:] == [h for h in history[-3:] if sig in h] and len(history) >= 3:
            print("Model is repeating itself. Stopping.")
            return

        draw_debug(img, action, step)
        if CONFIRM:
            if input("   run it? [y/N] ").strip().lower() != "y":
                print("Aborted by user.")
                return
        else:
            print(f"   running in {ACTION_DELAY}s (move mouse to top-left corner to abort)")
            time.sleep(ACTION_DELAY)

        status = execute(action, scale, img.size)
        history.append(f"{action.get('reason', '')} -> {status} {sig}")
        prev_img, last_acted = img, True

    print(f"Hit MAX_STEPS ({MAX_STEPS}) without finishing.")


if __name__ == "__main__":
    sw, sh = pyautogui.size()
    gw, gh = ImageGrab.grab().size
    print(f"Screen as pyautogui sees it: {sw}x{sh}; screenshot: {gw}x{gh}")
    if (sw, sh) != (gw, gh):
        print("  (they differ: Windows display scaling is in play. Clicks are scaled to pyautogui's size, so this is handled.)")
    if sys.argv[1:] == ["--calibrate"]:
        calibrate()
        sys.exit()
    goal = " ".join(sys.argv[1:]).strip()
    if not goal:
        goal = input("Goal: ").strip()
    if not goal:
        sys.exit("No goal given.")
    print("Starting in 3 seconds. Move mouse to top-left corner to abort.")
    time.sleep(3)
    if MINIMIZE_MAIN_MONITOR_FIRST:
        minimize_primary_monitor_windows()
        time.sleep(1.5)
    try:
        run(goal)
    except pyautogui.FailSafeException:
        print("Aborted (mouse hit the corner).")