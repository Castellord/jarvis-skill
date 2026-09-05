#!/usr/bin/env python3
"""Детерминированные операции Jarvis: правила, даты и выбор One Job.

Работает на стандартной библиотеке. PyYAML используется, если доступен;
иначе включается встроенный парсер ограниченной схемы routing-rules.yaml.
Сеть не нужна, установка пакетов не нужна.

Команды:
  match       — сопоставить письмо с routing-rules.yaml (уровень 2)
  add-noise   — добавить правило шума (фидбек "это шум")
  add-route   — добавить/обновить категорию роутинга (фидбек "это в проект X")
  init        — создать routing-rules.yaml с дефолтами
  dates       — посчитать статус задачи (overdue/due_soon/stale) с таймзоной
  one-job     — выбрать одну самую горящую активную задачу Linear из JSON
  selftest    — прогнать внутренние проверки

Все команды печатают JSON в stdout. Ошибки — JSON с полем "error", код возврата 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None


# --------------------------------------------------------------------------
# YAML: чтение (PyYAML или fallback) и запись (всегда своя, схема фиксирована)
# --------------------------------------------------------------------------

EMPTY = {
    "version": 1,
    "noise": [],
    "senders_hardened": [],
    "routing": {},
    "defaults": {"team": "", "project": ""},
}


def _fallback_load(text: str) -> dict:
    """Мини-парсер под фиксированную схему routing-rules.yaml.

    Поддерживает: скаляры, списки строк, inline-списки [a, b], вложенные
    отображения на 2/4 пробела, inline-мапы {k: v}. Этого достаточно для
    файлов, которые пишет этот же скрипт и шаблон из templates/.
    """

    def scalar(raw: str):
        raw = raw.strip()
        if raw.startswith("#") or raw == "":
            return None
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [scalar(p) for p in _split_top(inner)]
        if raw.startswith("{") and raw.endswith("}"):
            inner = raw[1:-1].strip()
            out: dict = {}
            if not inner:
                return out
            for part in _split_top(inner):
                if ":" not in part:
                    continue
                k, v = part.split(":", 1)
                out[k.strip().strip("\"'")] = scalar(v)
            return out
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            return raw[1:-1]
        if raw in ("null", "~"):
            return None
        if raw in ("true", "false"):
            return raw == "true"
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        return raw

    def _split_top(s: str) -> list:
        parts, depth, buf = [], 0, ""
        for ch in s:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(buf)
                buf = ""
            else:
                buf += ch
        if buf.strip():
            parts.append(buf)
        return [p.strip() for p in parts if p.strip()]

    def strip_comment(s: str) -> str:
        depth, quote = 0, ""
        for i, ch in enumerate(s):
            if quote:
                if ch == quote:
                    quote = ""
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "#" and depth == 0 and (i == 0 or s[i - 1] in " \t"):
                return s[:i]
        return s

    lines = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        body = strip_comment(raw_line).strip()
        if body:
            lines.append((indent, body))

    def parse_block(idx: int, indent: int):
        """Вернуть (значение, следующий_индекс)."""
        if idx >= len(lines):
            return None, idx
        if lines[idx][1].startswith("- "):
            items = []
            while idx < len(lines) and lines[idx][0] == indent and lines[idx][1].startswith("- "):
                body = lines[idx][1][2:].strip()
                idx += 1
                if not body:
                    val, idx = parse_block(idx, indent + 2)
                    items.append(val)
                elif body.startswith("{") or body.startswith("["):
                    items.append(scalar(body))
                elif ":" in body:
                    key, rest = body.split(":", 1)
                    items.append({key.strip().strip("\"'"): scalar(rest)})
                else:
                    items.append(scalar(body))
            return items, idx
        mapping: dict = {}
        while idx < len(lines) and lines[idx][0] == indent and not lines[idx][1].startswith("- "):
            line = lines[idx][1]
            if ":" not in line:
                idx += 1
                continue
            key, rest = line.split(":", 1)
            key = key.strip().strip("\"'")
            rest = rest.strip()
            idx += 1
            if rest:
                mapping[key] = scalar(rest)
            else:
                if idx < len(lines) and lines[idx][0] > indent:
                    val, idx = parse_block(idx, lines[idx][0])
                    mapping[key] = val
                else:
                    mapping[key] = None
        return mapping, idx

    parsed, _ = parse_block(0, lines[0][0] if lines else 0)
    return parsed or {}


def load_rules(path: str) -> dict:
    if not os.path.exists(path):
        return json.loads(json.dumps(EMPTY))
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = _yaml.safe_load(text) if _yaml else _fallback_load(text)
    if not isinstance(data, dict):
        data = {}
    for key, default in EMPTY.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    if data.get("routing") is None:
        data["routing"] = {}
    for key in ("noise", "senders_hardened"):
        if data.get(key) is None:
            data[key] = []
    return data


def _q(value) -> str:
    if value is None:
        return '""'
    return '"' + str(value).replace('"', '\\"') + '"'


def dump_rules(data: dict, path: str) -> None:
    """Канонический вывод. Комментарии пользователя не сохраняются —
    предупреждение об этом есть в references/feedback.md."""
    out = ["version: 1", ""]

    out.append("# Уровень 2: шум. Пополняется фидбеком \"это шум\".")
    if data.get("noise"):
        out.append("noise:")
        for rule in data["noise"]:
            match = rule.get("match", {}) if isinstance(rule, dict) else {}
            fields = []
            for key in ("from", "from_domain"):
                if match.get(key):
                    fields.append(f"{key}: {_q(match[key])}")
            if match.get("subject_contains"):
                items = ", ".join(_q(x) for x in match["subject_contains"])
                fields.append(f"subject_contains: [{items}]")
            note = f"  # {rule['note']}" if isinstance(rule, dict) and rule.get("note") else ""
            out.append("  - match: {" + ", ".join(fields) + "}" + note)
    else:
        out.append("noise: []")
    out.append("")

    out.append("# Фильтры, поднятые на сторону провайдера. Только аудит, на матчинг не влияет.")
    if data.get("senders_hardened"):
        out.append("senders_hardened:")
        for item in data["senders_hardened"]:
            fields = ", ".join(f"{k}: {_q(v)}" for k, v in item.items())
            out.append("  - {" + fields + "}")
    else:
        out.append("senders_hardened: []")
    out.append("")

    out.append("# Категории роутинга -> назначение в Linear.")
    routing = data.get("routing") or {}
    if routing:
        out.append("routing:")
        for name, cat in routing.items():
            out.append(f"  {name}:")
            kws = ", ".join(_q(x) for x in (cat.get("keywords") or []))
            doms = ", ".join(_q(x) for x in (cat.get("from_domains") or []))
            labels = ", ".join(_q(x) for x in (cat.get("labels") or []))
            out.append(f"    keywords: [{kws}]")
            out.append(f"    from_domains: [{doms}]")
            out.append(f"    team: {_q(cat.get('team', ''))}")
            out.append(f"    project: {_q(cat.get('project', ''))}")
            out.append(f"    labels: [{labels}]")
            out.append(f"    privacy: {cat.get('privacy', 'normal')}")
    else:
        out.append("routing: {}")
    out.append("")

    out.append("# Единственный источник правды для дефолтного назначения в Linear.")
    defaults = data.get("defaults") or {}
    out.append("defaults:")
    out.append(f"  team: {_q(defaults.get('team', ''))}")
    out.append(f"  project: {_q(defaults.get('project', ''))}")
    out.append("")

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Матчинг
# --------------------------------------------------------------------------


def _domain(address: str) -> str:
    address = (address or "").strip().lower()
    if "<" in address and ">" in address:
        address = address[address.rfind("<") + 1 : address.rfind(">")]
    return address.split("@")[-1] if "@" in address else ""


def _addr(address: str) -> str:
    address = (address or "").strip().lower()
    if "<" in address and ">" in address:
        address = address[address.rfind("<") + 1 : address.rfind(">")]
    return address


def match_email(rules: dict, sender: str, subject: str) -> dict:
    sender_addr, sender_dom = _addr(sender), _domain(sender)
    subj = (subject or "").lower()

    for rule in rules.get("noise") or []:
        crit = rule.get("match", {}) if isinstance(rule, dict) else {}
        if crit.get("from") and _addr(crit["from"]) == sender_addr:
            return {"verdict": "noise", "matched_on": "from", "level": 2}
        if crit.get("from_domain") and crit["from_domain"].strip().lower() == sender_dom:
            return {"verdict": "noise", "matched_on": "from_domain", "level": 2}
        for needle in crit.get("subject_contains") or []:
            if needle and needle.lower() in subj:
                return {"verdict": "noise", "matched_on": f"subject:{needle}", "level": 2}

    for name, cat in (rules.get("routing") or {}).items():
        cat = cat or {}
        for dom in cat.get("from_domains") or []:
            if dom and dom.strip().lower() == sender_dom:
                return _route(name, cat, f"from_domain:{dom}")
        for kw in cat.get("keywords") or []:
            if kw and kw.lower() in subj:
                return _route(name, cat, f"keyword:{kw}")

    defaults = rules.get("defaults") or {}
    return {
        "verdict": "unresolved",
        "level": 3,
        "next": "classification (LLM)",
        "fallback_team": defaults.get("team", ""),
        "fallback_project": defaults.get("project", ""),
    }


def _route(name: str, cat: dict, matched_on: str) -> dict:
    return {
        "verdict": "route",
        "level": 2,
        "category": name,
        "matched_on": matched_on,
        "team": cat.get("team", ""),
        "project": cat.get("project", ""),
        "labels": cat.get("labels") or [],
        "privacy": cat.get("privacy", "normal"),
    }


def resolve_category(rules: dict, category: str) -> dict:
    """Разрешить категорию, предложенную LLM (уровень 3), в team/project."""
    routing = rules.get("routing") or {}
    defaults = rules.get("defaults") or {}
    cat = routing.get(category)
    if cat:
        return {
            "source": "routing",
            "category": category,
            "team": cat.get("team", "") or defaults.get("team", ""),
            "project": cat.get("project", "") or defaults.get("project", ""),
            "labels": cat.get("labels") or [],
            "privacy": cat.get("privacy", "normal"),
        }
    return {
        "source": "defaults",
        "category": category,
        "team": defaults.get("team", ""),
        "project": defaults.get("project", ""),
        "labels": [],
        "privacy": "normal",
    }


# --------------------------------------------------------------------------
# Даты
# --------------------------------------------------------------------------


def _parse_dt(value: str):
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _plural_days(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def issue_status(due: str, updated: str, state: str, tz: str, now: str, stale_days: int) -> dict:
    tzinfo = timezone.utc
    if tz and ZoneInfo:
        try:
            tzinfo = ZoneInfo(tz)
        except Exception:
            tzinfo = timezone.utc
    now_dt = (_parse_dt(now) or datetime.now(timezone.utc)).astimezone(tzinfo)
    today = now_dt.date()

    state_norm = (state or "").strip().lower()
    if state_norm in ("done", "completed", "cancelled", "canceled"):
        return {"status": "closed", "detail": "", "include": False}

    due_dt = _parse_dt(due)
    if due_dt:
        due_date = due_dt.astimezone(tzinfo).date()
        delta = (due_date - today).days
        if delta < 0:
            return {
                "status": "overdue",
                "days": delta,
                "detail": f"Просрочено на {abs(delta)} {_plural_days(delta)}",
                "include": True,
            }
        if delta == 0:
            return {"status": "due_soon", "days": 0, "detail": "Дедлайн сегодня", "include": True}
        if delta == 1:
            return {"status": "due_soon", "days": 1, "detail": "Дедлайн завтра", "include": True}
        if delta <= 7:
            return {
                "status": "due_soon",
                "days": delta,
                "detail": f"Дедлайн через {delta} {_plural_days(delta)}",
                "include": True,
            }

    updated_dt = _parse_dt(updated)
    if updated_dt and state_norm in ("in progress", "started", "in_progress"):
        idle = (now_dt - updated_dt.astimezone(tzinfo)).days
        if idle >= stale_days:
            return {
                "status": "stale",
                "days": idle,
                "detail": f"Не трогали {idle} {_plural_days(idle)}",
                "include": True,
            }

    return {"status": "ok", "detail": "", "include": False}


# --------------------------------------------------------------------------
# One Job
# --------------------------------------------------------------------------


def _issue_field(issue: dict, *names, default=""):
    for name in names:
        value = issue.get(name)
        if value is not None and value != "":
            return value
    return default


def _priority_value(issue: dict) -> int:
    value = issue.get("priority", 0)
    if isinstance(value, dict):
        value = value.get("value", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _state_value(issue: dict) -> str:
    value = _issue_field(issue, "status", "state", default="")
    if isinstance(value, dict):
        value = value.get("name") or value.get("type") or ""
    return str(value)


def _one_job_rank(issue: dict, tz: str, now: str, stale_days: int) -> tuple:
    """Меньший tuple означает более срочную задачу."""
    state = _state_value(issue)
    due = str(_issue_field(issue, "dueDate", "due_date", default=""))
    updated = str(_issue_field(issue, "updatedAt", "updated_at", default=""))
    status = issue_status(due, updated, state, tz, now, stale_days)
    status_rank = {"overdue": 0, "due_soon": 1, "stale": 2, "ok": 3}.get(status["status"], 3)
    days = status.get("days", 10**9)
    date_rank = days if status["status"] in ("overdue", "due_soon", "stale") else 10**9
    priority = _priority_value(issue)
    priority_rank = priority if priority in (1, 2, 3, 4) else 5
    updated_dt = _parse_dt(updated)
    freshness_rank = -(updated_dt.timestamp()) if updated_dt else 0
    stable_id = str(_issue_field(issue, "identifier", "id", default=""))
    return status_rank, priority_rank, date_rank, freshness_rank, stable_id


def select_one_job(issues: list, tz: str, now: str, stale_days: int = 14) -> dict:
    """Отфильтровать активные задачи, убрать точные дубли и выбрать одну."""
    active = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        state = _state_value(issue).strip().lower()
        state_type = issue.get("statusType") or issue.get("stateType") or ""
        if isinstance(issue.get("state"), dict):
            state_type = issue["state"].get("type") or state_type
        if state in ("done", "completed", "cancelled", "canceled") or str(state_type).lower() in (
            "completed", "canceled", "cancelled"
        ):
            continue
        active.append(issue)

    unique = {}
    for issue in active:
        title = str(issue.get("title") or "").strip()
        key = title or str(_issue_field(issue, "identifier", "id", default=""))
        current = unique.get(key)
        if current is None or _one_job_rank(issue, tz, now, stale_days) < _one_job_rank(
            current, tz, now, stale_days
        ):
            unique[key] = issue

    if not unique:
        return {"ok": True, "selected": None, "active_count": 0, "unique_count": 0}

    selected = min(unique.values(), key=lambda item: _one_job_rank(item, tz, now, stale_days))
    due = str(_issue_field(selected, "dueDate", "due_date", default=""))
    updated = str(_issue_field(selected, "updatedAt", "updated_at", default=""))
    status = issue_status(due, updated, _state_value(selected), tz, now, stale_days)
    project = selected.get("project")
    if isinstance(project, dict):
        project = project.get("name") or project.get("id")
    return {
        "ok": True,
        "selected": {
            "id": _issue_field(selected, "identifier", "id", default=""),
            "linear_id": _issue_field(selected, "linear_id", "uuid", "id", default=""),
            "title": selected.get("title", ""),
            "url": selected.get("url", ""),
            "project": project or "",
            "priority": _priority_value(selected),
            "state": _state_value(selected),
            "due_date": due,
            "status": status["status"],
            "detail": status["detail"],
        },
        "active_count": len(active),
        "unique_count": len(unique),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_match(args) -> dict:
    rules = load_rules(args.rules)
    if args.category:
        return resolve_category(rules, args.category)
    return match_email(rules, args.sender or "", args.subject or "")


def cmd_add_noise(args) -> dict:
    rules = load_rules(args.rules)
    crit = {}
    if args.sender:
        crit["from"] = _addr(args.sender)
    if args.from_domain:
        crit["from_domain"] = args.from_domain.strip().lower()
    if args.subject_contains:
        crit["subject_contains"] = args.subject_contains
    if not crit:
        raise ValueError("нужен хотя бы один критерий: --from / --from-domain / --subject-contains")
    for existing in rules["noise"]:
        if existing.get("match") == crit:
            return {"ok": True, "changed": False, "reason": "правило уже есть", "rule": crit}
    entry = {"match": crit}
    if args.note:
        entry["note"] = args.note
    rules["noise"].append(entry)
    dump_rules(rules, args.rules)
    return {"ok": True, "changed": True, "rule": crit, "total_noise_rules": len(rules["noise"])}


def cmd_add_route(args) -> dict:
    rules = load_rules(args.rules)
    routing = rules.setdefault("routing", {})
    cat = routing.setdefault(
        args.category,
        {"keywords": [], "from_domains": [], "team": "", "project": "", "labels": [], "privacy": "normal"},
    )
    for kw in args.keyword or []:
        if kw not in (cat.get("keywords") or []):
            cat.setdefault("keywords", []).append(kw)
    for dom in args.from_domain or []:
        dom = dom.strip().lower()
        if dom not in (cat.get("from_domains") or []):
            cat.setdefault("from_domains", []).append(dom)
    if args.team:
        cat["team"] = args.team
    if args.project:
        cat["project"] = args.project
    if args.privacy:
        cat["privacy"] = args.privacy
    dump_rules(rules, args.rules)
    return {"ok": True, "category": args.category, "rule": cat}


def cmd_harden(args) -> dict:
    rules = load_rules(args.rules)
    rules.setdefault("senders_hardened", []).append(
        {"account_id": args.account, "from": _addr(args.sender), "filter_id": args.filter_id}
    )
    dump_rules(rules, args.rules)
    return {"ok": True, "total": len(rules["senders_hardened"])}


def cmd_init(args) -> dict:
    if os.path.exists(args.rules) and not args.force:
        rules = load_rules(args.rules)
        return {"ok": True, "created": False, "defaults": rules.get("defaults", {})}
    rules = json.loads(json.dumps(EMPTY))
    rules["defaults"] = {"team": args.team or "", "project": args.project or ""}
    dump_rules(rules, args.rules)
    return {"ok": True, "created": True, "path": args.rules, "defaults": rules["defaults"]}


def cmd_dates(args) -> dict:
    return issue_status(args.due, args.updated, args.state, args.tz, args.now, args.stale_days)


def cmd_one_job(args) -> dict:
    if args.issues_file == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.issues_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    if isinstance(data, dict):
        issues = data.get("issues") or data.get("nodes") or []
    else:
        issues = data
    if not isinstance(issues, list):
        raise ValueError("JSON должен быть массивом задач или объектом с полем issues/nodes")
    return select_one_job(issues, args.tz, args.now, args.stale_days)


def cmd_selftest(_args) -> dict:
    import tempfile

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append({"case": name, "got": got, "want": want})

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "routing-rules.yaml")

    cmd_init(argparse.Namespace(rules=path, team="T1", project="P1", force=True))
    rules = load_rules(path)
    check("init defaults", rules["defaults"], {"team": "T1", "project": "P1"})

    cmd_add_noise(
        argparse.Namespace(
            rules=path, sender=None, from_domain="news.example.com",
            subject_contains=None, note="дайджест",
        )
    )
    rules = load_rules(path)
    check(
        "noise matches domain",
        match_email(rules, "Robot <bot@news.example.com>", "Weekly")["verdict"],
        "noise",
    )
    check("noise ignores other domain", match_email(rules, "a@other.com", "Weekly")["verdict"], "unresolved")

    dup = cmd_add_noise(
        argparse.Namespace(
            rules=path, sender=None, from_domain="news.example.com",
            subject_contains=None, note=None,
        )
    )
    check("noise dedup", dup["changed"], False)

    cmd_add_route(
        argparse.Namespace(
            rules=path, category="home", keyword=["отключение"], from_domain=[],
            team="T2", project="P2", privacy="normal",
        )
    )
    rules = load_rules(path)
    routed = match_email(rules, "uk@dom.ru", "Отключение электричества 22.08")
    check("route by keyword", (routed["verdict"], routed["team"]), ("route", "T2"))
    check("route case-insensitive", match_email(rules, "uk@dom.ru", "ОТКЛЮЧЕНИЕ воды")["verdict"], "route")

    check("resolve known category", resolve_category(rules, "home")["team"], "T2")
    check("resolve unknown category", resolve_category(rules, "medical")["source"], "defaults")

    now = "2026-08-21T09:00:00+03:00"
    check(
        "overdue",
        issue_status("2026-08-18", "", "Todo", "Europe/Moscow", now, 14)["detail"],
        "Просрочено на 3 дня",
    )
    check(
        "due today",
        issue_status("2026-08-21", "", "Todo", "Europe/Moscow", now, 14)["status"],
        "due_soon",
    )
    check(
        "done excluded",
        issue_status("2026-01-01", "", "Done", "Europe/Moscow", now, 14)["include"],
        False,
    )
    check(
        "stale",
        issue_status("", "2026-08-01T00:00:00Z", "In Progress", "Europe/Moscow", now, 14)["status"],
        "stale",
    )
    check(
        "not stale",
        issue_status("", "2026-08-19T00:00:00Z", "In Progress", "Europe/Moscow", now, 14)["status"],
        "ok",
    )
    check("plural 1", _plural_days(1), "день")
    check("plural 11", _plural_days(11), "дней")
    check("plural 22", _plural_days(22), "дня")

    jobs = [
        {"id": "closed", "identifier": "BRA-1", "title": "Закрытая", "priority": 1,
         "dueDate": "2025-01-01", "status": "Done"},
        {"id": "soon", "identifier": "BRA-2", "title": "Скоро", "priority": 1,
         "dueDate": "2026-08-21", "status": "Todo"},
        {"id": "late-high", "identifier": "BRA-3", "title": "Просроченная", "priority": 2,
         "dueDate": "2026-08-10", "status": "Todo"},
        {"id": "late-urgent", "identifier": "BRA-4", "title": "Просроченная срочная", "priority": 1,
         "dueDate": "2026-08-18", "status": "Todo"},
    ]
    picked = select_one_job(jobs, "Europe/Moscow", now)
    check("one job excludes closed and picks overdue urgent", picked["selected"]["id"], "BRA-4")
    check("one job preserves Linear UUID", picked["selected"]["linear_id"], "late-urgent")
    normalized_jobs = [
        {"id": "BRA-7", "identifier": "BRA-7", "linear_id": "uuid-7",
         "title": "Нормализованная", "priority": 1,
         "dueDate": "2026-08-17", "status": "Todo"},
    ]
    normalized = select_one_job(normalized_jobs, "Europe/Moscow", now)
    check("one job prefers normalized linear_id", normalized["selected"]["linear_id"], "uuid-7")
    duplicate_jobs = [
        {"id": "a", "identifier": "BRA-5", "title": "Дубль", "priority": 2,
         "dueDate": "2026-08-20", "status": "Todo"},
        {"id": "b", "identifier": "BRA-6", "title": "Дубль", "priority": 1,
         "dueDate": "2026-08-19", "status": "Todo"},
    ]
    deduped = select_one_job(duplicate_jobs, "Europe/Moscow", now)
    check("one job deduplicates exact titles", deduped["unique_count"], 1)
    check("one job keeps strongest duplicate", deduped["selected"]["id"], "BRA-6")
    check("one job empty", select_one_job([], "Europe/Moscow", now)["selected"], None)

    # Fallback-парсер должен читать то, что пишет dump_rules.
    with open(path, "r", encoding="utf-8") as fh:
        fallback = _fallback_load(fh.read())
    check("fallback parser defaults", fallback.get("defaults"), {"team": "T1", "project": "P1"})
    check(
        "fallback parser routing",
        match_email(fallback, "uk@dom.ru", "Отключение воды")["verdict"],
        "route",
    )

    return {"ok": not failures, "failures": failures, "checks_run": 22}


def main() -> int:
    parser = argparse.ArgumentParser(description="Детерминированные операции Jarvis")
    default_rules = os.environ.get("JARVIS_RULES", "assets/routing-rules.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("match", help="сопоставить письмо с правилами уровня 2")
    p.add_argument("--rules", default=default_rules)
    p.add_argument("--from", dest="sender")
    p.add_argument("--subject")
    p.add_argument("--category", help="вместо письма: разрешить категорию от LLM в team/project")
    p.set_defaults(fn=cmd_match)

    p = sub.add_parser("add-noise", help='фидбек "это шум"')
    p.add_argument("--rules", default=default_rules)
    p.add_argument("--from", dest="sender")
    p.add_argument("--from-domain")
    p.add_argument("--subject-contains", nargs="*")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_add_noise)

    p = sub.add_parser("add-route", help='фидбек "это в проект X"')
    p.add_argument("--rules", default=default_rules)
    p.add_argument("--category", required=True)
    p.add_argument("--keyword", nargs="*")
    p.add_argument("--from-domain", nargs="*")
    p.add_argument("--team")
    p.add_argument("--project")
    p.add_argument("--privacy", choices=["normal", "strict"])
    p.set_defaults(fn=cmd_add_route)

    p = sub.add_parser("harden", help="записать созданный провайдерский фильтр в аудит")
    p.add_argument("--rules", default=default_rules)
    p.add_argument("--account", required=True)
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--filter-id", required=True)
    p.set_defaults(fn=cmd_harden)

    p = sub.add_parser("init", help="создать routing-rules.yaml")
    p.add_argument("--rules", default=default_rules)
    p.add_argument("--team")
    p.add_argument("--project")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("dates", help="статус задачи с учётом таймзоны")
    p.add_argument("--due", default="")
    p.add_argument("--updated", default="")
    p.add_argument("--state", default="")
    p.add_argument("--tz", default=os.environ.get("JARVIS_TZ", "UTC"))
    p.add_argument("--now", default="")
    p.add_argument("--stale-days", type=int, default=14)
    p.set_defaults(fn=cmd_dates)

    p = sub.add_parser("one-job", help="выбрать одну самую горящую задачу Linear из JSON")
    p.add_argument("--issues-file", required=True, help="JSON-файл или - для stdin")
    p.add_argument("--tz", default=os.environ.get("JARVIS_TZ", "UTC"))
    p.add_argument("--now", default="")
    p.add_argument("--stale-days", type=int, default=14)
    p.set_defaults(fn=cmd_one_job)

    p = sub.add_parser("selftest", help="внутренние проверки")
    p.set_defaults(fn=cmd_selftest)

    args = parser.parse_args()
    try:
        result = args.fn(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
