import json
import os
import subprocess
import sys
import threading
import time

import requests

START_SEAT = 2969567
END_SEAT = 2980000
BASE_URL = "https://nategafany.com/api/result.php"

STATE_FILE = "checkpoint.json"
RESULTS_FILE = "student_results.json"
TMP_STATE = "checkpoint.tmp"
TMP_RESULTS = "results.tmp"

MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "1380"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.2"))
WORKERS = int(os.environ.get("WORKERS", "4"))
SAVE_INTERVAL = float(os.environ.get("SAVE_INTERVAL", "150"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def in_git_repo():
    try:
        return git("rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    except Exception:
        return False


def current_branch():
    br = os.environ.get("GITHUB_REF_NAME", "").strip()
    if br and br != "HEAD":
        return br
    name = git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    if name and name != "HEAD":
        return name
    return "main"


def save_state(state, commit):
    with open(TMP_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(TMP_STATE, STATE_FILE)

    sorted_results = sorted(state["results"], key=lambda x: x["percentage"], reverse=True)
    with open(TMP_RESULTS, "w", encoding="utf-8") as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=4)
    os.replace(TMP_RESULTS, RESULTS_FILE)

    if not commit:
        return
    if not in_git_repo():
        print("(Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ù…Ø³ØªÙˆØ¯Ø¹ git Ù‡Ù†Ø§ â€” ØªÙ… Ø­ÙØ¸ Ø§Ù„Ù…Ù„ÙØ§Øª ÙÙ‚Ø· Ø¯ÙˆÙ† commit)")
        return

    git("add", "-A")
    changed = git("diff", "--cached", "--quiet", check=False)
    if changed.returncode != 0:
        branch = current_branch()
        git("commit", "-m", f"checkpoint: seat {state.get('last_done_seat')} - {len(state['results'])} results")
        for attempt in range(5):
            pull = git("pull", "--rebase", "origin", branch, check=False)
            if pull.returncode != 0:
                print(f"âš ï¸ git pull ÙØ´Ù„: {pull.stderr.strip()[:200]} â€” Ù…Ø­Ø§ÙˆÙ„Ø© Ø£Ø®Ø±Ù‰...")
                time.sleep(10)
                continue
            push = git("push", "origin", branch, check=False)
            if push.returncode == 0:
                break
            print(f"âš ï¸ git push ÙØ´Ù„: {push.stderr.strip()[:200]} â€” Ù…Ø­Ø§ÙˆÙ„Ø© Ø£Ø®Ø±Ù‰...")
            time.sleep(10)


class Scraper:
    def __init__(self):
        self.lock = threading.Lock()
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = {"next_seat": START_SEAT, "last_done_seat": None, "results": []}

        seen = {}
        deduped = []
        for r in state.get("results", []):
            sn = r.get("seat_no")
            if sn not in seen:
                seen[sn] = True
                deduped.append(r)
        state["results"] = deduped

        self.state = state
        self.known = {r["seat_no"] for r in state["results"]}
        self.next = int(state.get("next_seat", START_SEAT))
        print(f"[startup] next_seat={self.next} | Ù†ØªØ§Ø¦Ø¬ Ù…Ø­Ù…Ù„Ø©: {len(self.known)}")
        self.ban_until = 0.0
        self.start = time.monotonic()
        self.finished = False
        self.git_lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def persist(self, commit=False, last_done=None):
        st = self.snapshot()
        st["next_seat"] = self.next
        if last_done is not None:
            st["last_done_seat"] = last_done
        if self.finished:
            st["done"] = True
        with self.git_lock:
            save_state(st, commit)

    def take_seat(self):
        with self.lock:
            if self.next > END_SEAT:
                return None
            s = self.next
            self.next += 1
            return s

    def wait_banned(self):
        while True:
            with self.lock:
                until = self.ban_until
            wait = until - time.time()
            if wait <= 0:
                return
            time.sleep(min(wait, 1.0))

    def mark_banned(self):
        with self.lock:
            self.ban_until = max(self.ban_until, time.time() + 60)

    def worker(self, session):
        while True:
            if time.monotonic() - self.start > MAX_RUNTIME_MIN * 60:
                return

            seat = self.take_seat()
            if seat is None:
                return
            if seat in self.known:
                continue

            seat_no = seat
            while True:
                self.wait_banned()
                try:
                    response = session.get(BASE_URL, params={"seat_no": seat_no}, timeout=10)

                    if response.status_code == 429:
                        print(f"âš ï¸ ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ù„Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰ Ø¹Ù†Ø¯ Ø§Ù„Ø±Ù‚Ù… {seat_no}. ØªØ¨Ø§Ø·Ø¤ Ù…Ø¤Ù‚Øª...")
                        self.mark_banned()
                        time.sleep(10)
                        continue

                    if response.status_code == 200:
                        payload = response.json()
                        if payload.get("status") == "success" and "data" in payload and payload["data"].get("name"):
                            data = payload["data"]
                            pct_str = data.get("percentage", "0%")
                            student_info = {
                                "seat_no": seat_no,
                                "name": data.get("name", "ØºÙŠØ± Ù…Ø¹Ø±ÙˆÙ"),
                                "school": data.get("school", "-"),
                                "division": data.get("division", "-"),
                                "specialization": data.get("specialization", "-"),
                                "score": data.get("total", "0"),
                                "grade": data.get("grade", "-"),
                                "percentage": float(pct_str.replace("%", "").strip()),
                                "pct_str": pct_str,
                            }
                            with self.lock:
                                self.state["results"].append(student_info)
                            print(f"âœ“ ØªÙ… Ø³Ø­Ø¨ {seat_no}: {student_info['name']} | {student_info['score']} ({pct_str}) | {student_info['division']} - {student_info['specialization']} | {student_info['school']}")
                        else:
                            print(f"âœ— Ù„Ø§ ØªÙˆØ¬Ø¯ Ø¨ÙŠØ§Ù†Ø§Øª Ù„Ø±Ù‚Ù… Ø§Ù„Ø¬Ù„ÙˆØ³ {seat_no}")
                        break
                    elif response.status_code == 404:
                        print(f"âœ— Ø±Ù‚Ù… Ø§Ù„Ø¬Ù„ÙˆØ³ {seat_no} ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯ (404)")
                        break
                    else:
                        print(f"âœ— Ø®Ø·Ø£ Ø±Ù…Ø² ({response.status_code}) Ù„Ø±Ù‚Ù… Ø§Ù„Ø¬Ù„ÙˆØ³ {seat_no}")
                        break

                except Exception as e:
                    print(f"âš ï¸ Ø®Ø·Ø£ ÙÙŠ Ø§Ù„Ø§ØªØµØ§Ù„ Ø¹Ù†Ø¯ Ø§Ù„Ø±Ù‚Ù… {seat_no}: {e}. Ø¥Ø¹Ø§Ø¯Ø© Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø© Ø¨Ø¹Ø¯ 3 Ø«ÙˆØ§Ù†Ù...")
                    time.sleep(3)

            time.sleep(REQUEST_DELAY)

    def saver(self):
        while True:
            time.sleep(SAVE_INTERVAL)
            if self.finished:
                return
            with self.lock:
                last = self.next - 1
            self.persist(commit=True, last_done=last)
            print(f"ðŸ’¾ Ø­ÙØ¸ Ø§Ù„ØªÙ‚Ø¯Ù…: Ø¢Ø®Ø± Ø±Ù‚Ù… {last} â€” Ø§Ù„Ù†ØªØ§Ø¦Ø¬: {len(self.snapshot()['results'])}")

    def run(self):
        if self.next > END_SEAT:
            print(f"Ø§Ù„Ù…Ø³Ø­ Ù…ÙƒØªÙ…Ù„ Ø¨Ø§Ù„ÙØ¹Ù„ (next_seat={self.next}). Ù„Ø§ Ø´ÙŠØ¡ Ù„ÙØ¹Ù„Ù‡.")
            return

        print(f"Ø¨Ø¯Ø¡ Ø§Ù„Ù…Ø³Ø­ Ø§Ù„Ù…ØªÙˆØ§Ø²ÙŠ ({WORKERS} Ø¹Ù…Ø§Ù„) Ù…Ù† Ø§Ù„Ø±Ù‚Ù… {self.next} Ø­ØªÙ‰ {END_SEAT}...")
        session = requests.Session()
        threads = []
        for _ in range(WORKERS):
            t = threading.Thread(target=self.worker, args=(session,), daemon=True)
            t.start()
            threads.append(t)

        saver = threading.Thread(target=self.saver, daemon=True)
        saver.start()

        for t in threads:
            t.join()

        self.finished = True
        saver.join(timeout=10)
        self.persist(commit=True, last_done=min(self.next - 1, END_SEAT))

        with self.lock:
            total = len(self.state["results"])
        if self.next > END_SEAT:
            print("\nâœ… Ø§ÙƒØªÙ…Ù„ Ø§Ù„Ù…Ø³Ø­ Ø¨Ø§Ù„ÙƒØ§Ù…Ù„!")
        else:
            print(f"\nâ° Ø§Ù†ØªÙ‡Ù‰ Ø§Ù„ÙˆÙ‚Øª Ø§Ù„Ù…Ø³Ù…ÙˆØ­ ({MAX_RUNTIME_MIN} Ø¯Ù‚ÙŠÙ‚Ø©). Ø§Ù„Ø¬ÙˆØ¨ Ø§Ù„Ù‚Ø§Ø¯Ù… Ø³ÙŠÙƒÙ…Ù„ ØªÙ„Ù‚Ø§Ø¦ÙŠÙ‹Ø§.")
        print(f"Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ø·Ù„Ø§Ø¨: {total}")


if __name__ == "__main__":
    Scraper().run()
