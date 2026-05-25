# Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 12 security fixes for the Cove video downloader bot, covering error sanitization, file permissions, rate limiting, input validation, and logging.

**Architecture:** All changes go into `bot.py` (single-file bot). New constants and helpers are added near existing ones. Per-user rate limiting uses an in-memory dict with TTL-based pruning (same pattern as `_deletable` and `_friend_posts`). Security logging uses the existing `log` logger with a `[security]` prefix for grep-ability.

**Tech Stack:** Python 3, discord.py, aiohttp, asyncio

---

### Task 1: Sanitize fallback error messages (Critical)

**Files:**
- Modify: `bot.py:770-772` (video download error fallback)
- Modify: `bot.py:918-920` (audio download error fallback)
- Test: `tests/test_security.py`

The fallback `last_error` on lines 771 and 919 sends the raw last line of yt-dlp/ffmpeg stderr to Discord. This can leak system paths, cookie paths, auth errors, or internal hostnames.

- [ ] **Step 1: Write the failing test**

Create `tests/test_security.py`:

```python
from bot import _sanitize_error_line


def test_sanitize_strips_filesystem_paths():
    raw = "ERROR: /home/user/.config/yt-dlp/cookies.txt: file not found"
    result = _sanitize_error_line(raw)
    assert "/home/" not in result
    assert "cookies" not in result.lower()


def test_sanitize_strips_ip_addresses():
    raw = "ERROR: Unable to connect to 192.168.1.50:8080"
    result = _sanitize_error_line(raw)
    assert "192.168" not in result


def test_sanitize_strips_env_variable_references():
    raw = "ERROR: DISCORD_TOKEN=abc123 invalid"
    result = _sanitize_error_line(raw)
    assert "abc123" not in result


def test_sanitize_preserves_clean_error():
    raw = "ERROR: Video unavailable"
    result = _sanitize_error_line(raw)
    assert result == "Video unavailable"


def test_sanitize_fallback_on_empty():
    assert _sanitize_error_line("") == "Download failed."
    assert _sanitize_error_line("   ") == "Download failed."


def test_sanitize_strips_error_prefix():
    raw = "ERROR: Something went wrong"
    result = _sanitize_error_line(raw)
    assert result == "Something went wrong"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "sanitize" 2>&1 | head -30`
Expected: FAIL with ImportError (function doesn't exist yet)

- [ ] **Step 3: Write the sanitization function**

Add after line 419 (after `ENV = clean_env()`) in `bot.py`:

```python
_SENSITIVE_PATTERNS = re.compile(
    r"/(?:home|usr|tmp|etc|var|dev|root|proc|sys)/\S+"       # filesystem paths
    r"|[A-Z_]{2,}=\S+"                                       # ENV_VAR=value
    r"|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"                # IPv4 addresses
    r"|\b[0-9a-fA-F]{32,}\b"                                 # long hex tokens
    r"|(?:cookie|token|secret|password|credential)\S*"        # credential keywords
    , re.IGNORECASE,
)

def _sanitize_error_line(raw: str) -> str:
    line = raw.strip()
    if not line:
        return "Download failed."
    if line.upper().startswith("ERROR:"):
        line = line[6:].strip()
    if not line:
        return "Download failed."
    cleaned = _SENSITIVE_PATTERNS.sub("[redacted]", line)
    if cleaned.strip() == "[redacted]" or not cleaned.strip():
        return "Download failed."
    return cleaned
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "sanitize"`
Expected: All 6 tests PASS

- [ ] **Step 5: Apply sanitization to both download functions**

In `bot.py`, replace the fallback error lines in `download_and_compress` (around line 771):

```python
# OLD:
            last_error = out.strip().splitlines()[-1] if out.strip() else "Unknown error."
            _log.append(f"[ERROR] {last_error}")
# NEW:
            raw_last = out.strip().splitlines()[-1] if out.strip() else ""
            _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
```

Same change in `download_audio` (around line 919):

```python
# OLD:
                last_error = out.strip().splitlines()[-1] if out.strip() else "Unknown error."
                _log.append(f"[ERROR] {last_error}")
# NEW:
                raw_last = out.strip().splitlines()[-1] if out.strip() else ""
                _log.append(f"[ERROR] {_sanitize_error_line(raw_last)}")
```

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/test_security.py
git commit -m "Sanitize fallback error messages to prevent info leakage"
```

---

### Task 2: Restrict cookies.txt file permissions at startup (Critical)

**Files:**
- Modify: `bot.py:64-65` (after COOKIES_FILE/COOKIES_EXIST definitions)

- [ ] **Step 1: Add permission hardening after cookies detection**

After line 65 (`COOKIES_EXIST = ...`), add:

```python
if COOKIES_EXIST:
    try:
        os.chmod(COOKIES_FILE, 0o600)
    except OSError as e:
        log.warning("[Cove] Could not restrict cookies.txt permissions: %s", e)
```

- [ ] **Step 2: Verify manually**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -c "import os; os.chmod('cookies.txt', 0o644)" && stat -c '%a' cookies.txt && python -c "
import os
os.chmod('cookies.txt', 0o600)
print(oct(os.stat('cookies.txt').st_mode)[-3:])
"`
Expected: First prints `644`, then prints `600`

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "Restrict cookies.txt to owner-only at startup"
```

---

### Task 3: Add per-user rate limiting (High)

**Files:**
- Modify: `bot.py` (new constants, rate limit dict, check function, integration into on_message and slash commands)
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_security.py`:

```python
from bot import _check_user_rate_limit, USER_RATE_LIMIT, USER_RATE_WINDOW


def test_rate_limit_allows_first_request():
    assert _check_user_rate_limit(99999) is True


def test_rate_limit_blocks_after_exceeded():
    user_id = 88888
    for _ in range(USER_RATE_LIMIT):
        assert _check_user_rate_limit(user_id) is True
    assert _check_user_rate_limit(user_id) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "rate_limit" 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement rate limiting**

Add constants near other config constants (after line 78):

```python
USER_RATE_LIMIT  = _require_int_env("USER_RATE_LIMIT", default="10")
USER_RATE_WINDOW = _require_int_env("USER_RATE_WINDOW", default="60")
```

Add the rate limit state and function after the `_friend_neet_skip_users` dict (after line 201):

```python
_user_request_times: dict[int, list[float]] = {}


def _check_user_rate_limit(user_id: int) -> bool:
    now = monotonic()
    times = _user_request_times.get(user_id, [])
    times = [t for t in times if now - t < USER_RATE_WINDOW]
    if len(times) >= USER_RATE_LIMIT:
        _user_request_times[user_id] = times
        return False
    times.append(now)
    _user_request_times[user_id] = times
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "rate_limit"`
Expected: Both tests PASS

- [ ] **Step 5: Integrate into on_message**

In `on_message`, after the URL extraction check (`if not url: return`) at line 1128, add:

```python
        if not _check_user_rate_limit(message.author.id):
            log.warning("[security] Rate limit hit for user %d in #%s", message.author.id, message.channel)
            return
```

- [ ] **Step 6: Integrate into slash commands**

In `download_cmd` (after `validate_manual_url` passes, before `defer`), add:

```python
    if not _check_user_rate_limit(interaction.user.id):
        try:
            await interaction.response.send_message(
                "❌ You're sending too many requests. Please wait a moment.",
                ephemeral=True,
            )
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        return
```

Same in `audio_cmd` (after `validate_manual_url` passes, before `defer`).

- [ ] **Step 7: Commit**

```bash
git add bot.py tests/test_security.py
git commit -m "Add per-user rate limiting (10 requests/60s)"
```

---

### Task 4: Validate yt-dlp output filenames (High)

**Files:**
- Modify: `bot.py` (new sanitize function, apply in download pipelines)
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_security.py`:

```python
from bot import _sanitize_filename


def test_filename_strips_path_traversal():
    assert ".." not in _sanitize_filename("../../etc/passwd.mp4")


def test_filename_strips_null_bytes():
    assert "\x00" not in _sanitize_filename("video\x00.mp4")


def test_filename_preserves_normal_name():
    assert _sanitize_filename("My Cool Video.mp4") == "My Cool Video.mp4"


def test_filename_limits_length():
    long_name = "a" * 300 + ".mp4"
    result = _sanitize_filename(long_name)
    assert len(result) <= 200


def test_filename_fallback_on_empty():
    result = _sanitize_filename("")
    assert result == "video"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "filename" 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement filename sanitization**

Add after `_sanitize_error_line` in `bot.py`:

```python
def _sanitize_filename(name: str) -> str:
    if not name or not name.strip():
        return "video"
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    name = name.replace("..", "_")
    parts = name.rsplit(".", 1)
    stem = parts[0][:190]
    ext = parts[1] if len(parts) > 1 else ""
    if ext:
        return f"{stem}.{ext[:10]}"
    return stem if stem else "video"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/sin/bots/cove-video-downloader-bot && python -m pytest tests/test_security.py -v -k "filename"`
Expected: All 5 tests PASS

- [ ] **Step 5: Apply to download pipelines**

In `download_and_compress`, after `src_path = str(mp4_files[0])` (line 783), add filename sanitization for the Discord upload:

```python
    safe_name = _sanitize_filename(mp4_files[0].name)
    if mp4_files[0].name != safe_name:
        safe_path = str(mp4_files[0].parent / safe_name)
        os.rename(src_path, safe_path)
        src_path = safe_path
```

In `download_audio`, after `audio_path = str(mp3_files[0])` (line 929), same treatment:

```python
        safe_name = _sanitize_filename(mp3_files[0].name)
        if mp3_files[0].name != safe_name:
            safe_path = str(mp3_files[0].parent / safe_name)
            os.rename(audio_path, safe_path)
            audio_path = safe_path
```

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/test_security.py
git commit -m "Sanitize yt-dlp output filenames before use"
```

---

### Task 5: Add Discord permission pre-checks (High)

**Files:**
- Modify: `bot.py` (new helper, apply in on_message before processing)

- [ ] **Step 1: Add permission check helper**

Add after the `is_friend_server` function (around line 403):

```python
def _check_bot_permissions(channel: discord.abc.GuildChannel, bot_member: discord.Member) -> tuple[bool, str]:
    perms = channel.permissions_for(bot_member)
    missing = []
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.attach_files:
        missing.append("Attach Files")
    if not perms.add_reactions:
        missing.append("Add Reactions")
    if missing:
        return False, ", ".join(missing)
    return True, ""
```

- [ ] **Step 2: Integrate into on_message**

In `on_message`, after the URL extraction and rate limit check, before the hourglass reaction, add:

```python
        if message.guild and message.guild.me:
            perms_ok, missing = _check_bot_permissions(message.channel, message.guild.me)
            if not perms_ok:
                log.warning("[security] Missing permissions in #%s: %s", message.channel, missing)
                return
```

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "Add Discord permission pre-checks before processing"
```

---

### Task 6: Pin yt-dlp version check (Medium)

**Files:**
- Modify: `bot.py` (startup version check)

- [ ] **Step 1: Add version check at startup**

Add a new constant and startup check after `USE_NVENC` (around line 80):

```python
YT_DLP_MIN_VERSION = os.getenv("YT_DLP_MIN_VERSION", "2024.01.01")
```

Add a startup check in the module body (after `ENV = clean_env()`, line 419):

```python
def _check_ytdlp_version():
    import subprocess
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=5, env=ENV,
        )
        version = result.stdout.strip()
        if version < YT_DLP_MIN_VERSION:
            log.warning(
                "[security] yt-dlp version %s is older than minimum %s",
                version, YT_DLP_MIN_VERSION,
            )
        else:
            log.info("[Cove] yt-dlp version: %s", version)
    except Exception as e:
        log.warning("[security] Could not check yt-dlp version: %s", e)

_check_ytdlp_version()
```

- [ ] **Step 2: Commit**

```bash
git add bot.py
git commit -m "Add yt-dlp version check at startup"
```

---

### Task 7: Limit Reddit JSON response size (Medium)

**Files:**
- Modify: `bot.py:517-523` (`reddit_has_video` function)
- Modify: `bot.py:550-555` (`resolve_arazu` function)

- [ ] **Step 1: Add response size constant**

Add near the other constants (after `CACHE_MAX_ENTRIES`):

```python
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024  # 1 MB
```

- [ ] **Step 2: Apply to reddit_has_video**

Replace the unbounded `resp.text()` call in `reddit_has_video` (line 523):

```python
# OLD:
            raw = await resp.text(errors="replace")
# NEW:
            body = await resp.content.read(MAX_HTTP_RESPONSE_BYTES)
            raw = body.decode(errors="replace")
```

- [ ] **Step 3: Apply to resolve_arazu**

Replace the unbounded `resp.text()` call in `resolve_arazu` (line 555):

```python
# OLD:
            html = await resp.text(errors="replace")
# NEW:
            body = await resp.content.read(MAX_HTTP_RESPONSE_BYTES)
            html = body.decode(errors="replace")
```

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "Limit HTTP response body reads to 1MB"
```

---

### Task 8: Add Content-Type validation on HTTP responses (Medium)

**Files:**
- Modify: `bot.py:517-524` (`reddit_has_video` — check for JSON content-type)

- [ ] **Step 1: Add content-type check to reddit_has_video**

In `reddit_has_video`, after opening the response context manager, before reading body, add:

```python
            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type and "text" not in content_type:
                log.warning("[security] Reddit API returned unexpected Content-Type: %s", content_type)
                return True
```

- [ ] **Step 2: Commit**

```bash
git add bot.py
git commit -m "Validate Content-Type on Reddit API responses"
```

---

### Task 9: Harden temp directory permissions (Medium)

**Files:**
- Modify: `bot.py:686` (in `download_and_compress`)
- Modify: `bot.py:841` (in `download_audio`)

- [ ] **Step 1: Set 0o700 on temp dirs after creation**

In `download_and_compress`, after `tmp = tempfile.mkdtemp(...)` (line 686), add:

```python
    os.chmod(tmp, 0o700)
```

In `download_audio`, after `tmp = tempfile.mkdtemp(...)` (line 841), add:

```python
        os.chmod(tmp, 0o700)
```

- [ ] **Step 2: Commit**

```bash
git add bot.py
git commit -m "Set temp directories to 0o700 on creation"
```

---

### Task 10: Reduce orphan temp dir max age (Low)

**Files:**
- Modify: `bot.py:288` (`_sweep_orphaned_tmpdirs` default parameter)

- [ ] **Step 1: Change default min_age_seconds from 3600 to 900**

```python
# OLD:
def _sweep_orphaned_tmpdirs(min_age_seconds: float = 3600) -> None:
# NEW:
def _sweep_orphaned_tmpdirs(min_age_seconds: float = 900) -> None:
```

- [ ] **Step 2: Commit**

```bash
git add bot.py
git commit -m "Reduce orphan temp dir max age from 1h to 15min"
```

---

### Task 11: Add subprocess output size limits (Low)

**Files:**
- Modify: `bot.py:424-438` (`run_subprocess` function)

- [ ] **Step 1: Add output size constant and limit reads**

Add constant near other limits:

```python
MAX_SUBPROCESS_OUTPUT_BYTES = 512 * 1024  # 512 KB
```

Modify `run_subprocess` to limit the captured output:

```python
async def run_subprocess(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=ENV,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        stdout, _ = await proc.communicate()
        output = stdout[:MAX_SUBPROCESS_OUTPUT_BYTES].decode(errors="replace")
        return 124, output + "\n[ERROR] Subprocess timed out."
    return proc.returncode, stdout[:MAX_SUBPROCESS_OUTPUT_BYTES].decode(errors="replace")
```

- [ ] **Step 2: Commit**

```bash
git add bot.py
git commit -m "Limit subprocess output capture to 512KB"
```

---

### Task 12: Log security-relevant events (Low)

**Files:**
- Modify: `bot.py` (add logging at key decision points)

- [ ] **Step 1: Add security logging to URL validation**

In `validate_manual_url`, when validation fails, add logging before returning:

After `if _is_internal_ip(host):` check (line 390):

```python
    if _is_internal_ip(host):
        log.warning("[security] Blocked SSRF attempt to internal IP: %s", host)
        return False, "URL points to a non-public address."
```

After the DNS resolution check (line 397-398):

```python
    for info in infos:
        if _is_internal_ip(info[4][0]):
            log.warning("[security] Blocked SSRF attempt: %s resolved to internal IP %s", host, info[4][0])
            return False, "URL points to a non-public address."
```

- [ ] **Step 2: Add logging for blocked domains**

In `extract_supported_url`, after the blacklist check (line 351):

```python
        if host_matches(host, BLACKLISTED_DOMAINS):
            log.info("[security] Blocked blacklisted domain: %s", host)
            continue
```

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "Add security event logging for SSRF blocks and rate limits"
```

---

## Self-Review Checklist

1. **Spec coverage:** All 12 security fixes have dedicated tasks. Critical items (1-2) first, High (3-5), Medium (6-9), Low (10-12).
2. **Placeholder scan:** All code blocks contain complete implementations. No TBD/TODO.
3. **Type consistency:** `_sanitize_error_line`, `_sanitize_filename`, `_check_user_rate_limit`, `_check_bot_permissions` — names consistent across test and implementation steps.
4. **Line references:** All verified against current `bot.py` (1385 lines). Line numbers may shift as earlier tasks are applied — tasks should be applied in order.
