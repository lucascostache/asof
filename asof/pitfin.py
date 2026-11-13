"""PIT-Fin pilot harness: does a model answer questions about the future of a date it was told to stand at, and how much of that is memory?

A **question** has an ``as_of`` time T, a ``question`` about something that happened AFTER T, the ``answer``, and a ``control``: a comparable question about an event after every tested model's training cutoff
(so no model can remember it). Each question is asked four ways:

* ``A`` normal       the question as written.
* ``B`` disguised    the company is "Company A" and every date is shifted by a fixed, per-question number of days. A model that only *reasons* should do about as well as in A; one that *remembers* should do worse.
* ``C`` instruction  the question after "use only information that was public before T". The answer is an event after T, so a correct answer here means the model used what it should have set aside.
* ``D`` control      the control question: what the model can do with no memory to lean on (the **skill baseline**).

Scores per model (``score``): **memory gap** = accuracy(A) - accuracy(B); **instruction failure** = share of C answers that are correct anyway (and the drop from A to C: ``instruction_effect``);
**future-mention rate** = share of answers that mention a date after T or a fact that only someone who knew the later event could state; **skill baseline** = accuracy(D).

Every call goes through the model gateway, so it is stamped with T, its provider-side tools are refused unless allowed, and its receipt (hashes, tokens, flags) is kept. Grading is deterministic and
conservative (see ``grade``). Nothing here calls a provider by itself: the caller supplies the gateway, and ``scripts/pitfin_run.py`` refuses a real provider without ``--live``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clock import parse_time
from .errors import AsOfError, GatewayError
from .gateway import ModelGateway, ModelResponse

VARIANTS = ("A", "B", "C", "D")
PUBLIC_SOURCES = ("edgar", "prices", "earnings", "synthetic")          # public data only; "synthetic" marks the sample questions that ship with this package
CANNOT_ANSWER = "CANNOT_ANSWER"


class PitFinError(AsOfError, ValueError):
    """A question file or a question is malformed."""


@dataclass(frozen=True)
class Question:
    id: str
    as_of: str                                    # T, an ISO time with a zone
    question: str
    answer: str
    ticker: str
    event_date: str                               # when the thing asked about happened (after T)
    source: str                                   # "<kind>:<reference>", kind in PUBLIC_SOURCES
    company: str | None = None                    # the name to hide in the disguised variant
    later_facts: tuple[str, ...] = ()             # strings only someone who knew the later event could state
    tolerance: float = 0.0                        # relative tolerance for numeric answers: exact unless the question says otherwise (413 is not 412)
    control: "Question | None" = None             # the same kind of question about an event after every tested model's cutoff

    @property
    def synthetic(self) -> bool:
        return self.source.startswith("synthetic:")


# ---------------------------------------------------------------- loading
_REQUIRED = ("id", "as_of", "question", "answer", "ticker", "event_date", "source")


def _question(d: Mapping[str, Any], *, where: str, control_of: Question | None = None) -> Question:
    missing = [k for k in _REQUIRED if not str(d.get(k, "")).strip()]
    if missing:
        raise PitFinError(f"{where}: missing {missing}")
    src = str(d["source"]).strip()
    kind = src.partition(":")[0].lower()
    if kind not in PUBLIC_SOURCES or not src.partition(":")[2].strip():
        raise PitFinError(f"{where}: source {src!r} must be '<kind>:<reference>' with kind one of {PUBLIC_SOURCES} (public data only)")
    t = parse_time(d["as_of"], what=f"{where} as_of")
    try:
        ev = date.fromisoformat(str(d["event_date"])[:10])
    except ValueError:
        raise PitFinError(f"{where}: event_date {d['event_date']!r} is not YYYY-MM-DD") from None
    if ev <= t.date():
        raise PitFinError(f"{where}: event_date {ev} must be after as_of {t.date()}: the question is about something that had not happened yet")
    ctrl = d.get("control")
    if ctrl is not None and control_of is not None:
        raise PitFinError(f"{where}: a control cannot have a control")
    q = Question(str(d["id"]).strip(), t.isoformat().replace("+00:00", "Z"), str(d["question"]), str(d["answer"]), str(d["ticker"]).strip(), ev.isoformat(), src,
                 (str(d["company"]).strip() or None) if d.get("company") else None, tuple(str(x) for x in (d.get("later_facts") or [])), float(d.get("tolerance", 0.0)))
    if ctrl is not None:
        if not isinstance(ctrl, Mapping):
            raise PitFinError(f"{where}: control must be an object with the same fields as a question")
        c = _question({"id": f"{q.id}-control", "ticker": q.ticker, "source": q.source, **ctrl}, where=f"{where} control", control_of=q)
        object.__setattr__(q, "control", c)
    return q


def load_questions(path: str | Path) -> list[Question]:
    """Read a question set (JSON; YAML too if PyYAML is installed). Duplicate ids, a missing field, a non-public source, or an event that is not after T is an error that names the question."""
    p = Path(path).expanduser()
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise PitFinError("reading YAML needs PyYAML (pip install pyyaml); JSON needs nothing") from None
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PitFinError(f"{p.name} is not valid JSON: {e}") from None
    items = data.get("questions") if isinstance(data, Mapping) else data
    if not isinstance(items, list) or not items:
        raise PitFinError("a question set is a list of questions (or {\"questions\": [...]})")
    out, seen = [], set()
    for n, d in enumerate(items, 1):
        if not isinstance(d, Mapping):
            raise PitFinError(f"question {n} is not an object")
        q = _question(d, where=f"question {d.get('id', n)}")
        if q.id in seen:
            raise PitFinError(f"duplicate question id {q.id!r}")
        seen.add(q.id)
        out.append(q)
    return out


# ---------------------------------------------------------------- the four variants
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_LONG = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), (\d{4})\b")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def shift_days(q: Question) -> int:
    """The per-question date shift of the disguised variant: 90 to 359 days, from the question id (deterministic, so a rerun asks the same disguised question)."""
    return 90 + int(hashlib.sha256(q.id.encode()).hexdigest(), 16) % 270


def _shift_text(text: str, days: int) -> str:
    def iso(m: re.Match[str]) -> str:
        return (date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=days)).isoformat()

    def long(m: re.Match[str]) -> str:
        d = date(int(m[3]), _MONTHS.index(m[1]) + 1, int(m[2])) + timedelta(days=days)
        return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
    return _LONG.sub(long, _ISO.sub(iso, text))


def disguise(q: Question) -> Question:
    """The disguised question: the ticker and company name become "Company A" and every date (in the question, the answer, T and the event date) moves by the same number of days."""
    days = shift_days(q)
    t = q.question
    for name in sorted({n for n in (q.company, q.ticker) if n}, key=len, reverse=True):
        t = re.sub(rf"\b{re.escape(name)}\b", "Company A", t, flags=re.IGNORECASE)
    t = re.sub(r"Company A(['’]s)? \(Company A\)", r"Company A\1", t)                      # "Acme Corp (ACME)" became "Company A (Company A)": say it once
    as_of = parse_time(q.as_of, what="as_of") + timedelta(days=days)
    return Question(q.id, as_of.isoformat().replace("+00:00", "Z"), _shift_text(t, days), _shift_text(q.answer, days), "A", (date.fromisoformat(q.event_date) + timedelta(days=days)).isoformat(),
                    q.source, None, tuple(_shift_text(f, days) for f in q.later_facts), q.tolerance)


def instruction(q: Question) -> str:
    return (f"Use only information that was publicly available before {q.as_of[:10]}. Do not use anything you may know about events after that date. "
            f"If the answer depends on information from after that date, reply exactly: {CANNOT_ANSWER}\n\n")


@dataclass(frozen=True)
class Prompt:
    variant: str
    text: str
    expected: str
    as_of: str                                     # the T this prompt is about (shifted in B)
    later_facts: tuple[str, ...]
    tolerance: float


def build_prompt(q: Question, variant: str) -> Prompt | None:
    """The prompt for one variant, or ``None`` for D when the question has no control."""
    if variant == "A":
        return Prompt("A", q.question, q.answer, q.as_of, q.later_facts, q.tolerance)
    if variant == "B":
        d = disguise(q)
        return Prompt("B", d.question, d.answer, d.as_of, d.later_facts, d.tolerance)
    if variant == "C":
        return Prompt("C", instruction(q) + q.question, q.answer, q.as_of, q.later_facts, q.tolerance)
    if variant == "D":
        return Prompt("D", q.control.question, q.control.answer, q.control.as_of, q.control.later_facts, q.control.tolerance) if q.control else None
    raise PitFinError(f"variant must be one of {VARIANTS}, not {variant!r}")


# ---------------------------------------------------------------- grading
_NUM = re.compile(r"[-+]?\$?\d[\d,]*\.?\d*")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9.%$ ]+", " ", s.lower()).strip()


def _numbers(s: str) -> list[float]:
    out = []
    for m in _NUM.findall(s):
        try:
            out.append(float(m.replace("$", "").replace(",", "")))
        except ValueError:
            pass
    return out


def grade(expected: str, response: str, tolerance: float = 0.0) -> bool:
    """Is the response correct? Deterministic and conservative: ``CANNOT_ANSWER`` is wrong; a numeric answer is right if the response holds a number within ``tolerance`` (relative) of every number in the
    expected answer; a text answer is right if its normalised form appears as whole words in the normalised response. A hedge that names the right answer among several still counts only if it appears: grading is
    by what was said, not by what was meant."""
    if not response or CANNOT_ANSWER in response.upper():
        return False
    want = _numbers(expected)
    if want and not re.search(r"[A-Za-z]{3,}", re.sub(r"\b(million|billion|thousand|usd|eur|per|share|cents|percent|bn|m|k)\b", "", expected, flags=re.IGNORECASE)):
        have = _numbers(response)
        return all(any(abs(h - w) <= tolerance * abs(w) + 1e-9 for h in have) for w in want)
    e, r = _norm(expected), _norm(response)
    return bool(e) and re.search(rf"(?<![a-z0-9]){re.escape(e)}(?![a-z0-9])", r) is not None


def mentions_future(response: str, as_of: str, later_facts: Sequence[str] = ()) -> bool:
    """Does the response mention something after T: a date later than T (ISO or "Month D, YYYY"), or one of ``later_facts``?"""
    t = parse_time(as_of, what="as_of").date()
    for m in _ISO.finditer(response):
        try:
            if date(int(m[1]), int(m[2]), int(m[3])) > t:
                return True
        except ValueError:
            pass
    for m in _LONG.finditer(response):
        try:
            if date(int(m[3]), _MONTHS.index(m[1]) + 1, int(m[2])) > t:
                return True
        except ValueError:
            pass
    low = response.lower()
    return any(f.strip() and f.lower() in low for f in later_facts)


# ---------------------------------------------------------------- running
@dataclass
class Row:
    question_id: str
    model: str
    variant: str
    repeat: int
    correct: bool | None
    future_mention: bool | None
    refused: bool = False
    error: str | None = None
    response_sha256: str | None = None
    prompt_sha256: str | None = None
    call_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    flags: str = ""
    event_hash: str | None = None
    synthetic: bool = False


def run(questions: Sequence[Question], models: Sequence[str], *, repeats: int = 1, variants: Sequence[str] = VARIANTS, ledger: Any, gateway: ModelGateway | None = None,
        params: Mapping[str, Any] | None = None, on_row: Callable[[Row], None] | None = None) -> list[Row]:
    """Ask every question in every variant of every model ``repeats`` times, through the gateway. One session per call, stamped with the question's T, all on one ledger chain. A call the gateway refuses
    (a rule) or that fails is a row with ``error``, never a crash and never a silent skip."""
    from .session import Session
    if repeats < 1:
        raise PitFinError("repeats must be at least 1")
    rows: list[Row] = []
    for q in questions:
        for variant in variants:
            p = build_prompt(q, variant)
            if p is None:
                continue
            for model in models:
                for n in range(1, repeats + 1):
                    s = Session(q.as_of, ledger=ledger, gateway=gateway)
                    row = Row(q.id, model, variant, n, None, None, synthetic=q.synthetic)
                    try:
                        resp = s.model(model, messages=[{"role": "user", "content": p.text}], **dict(params or {}))
                        rec = s.model_receipt()
                        row.correct, row.future_mention = grade(p.expected, resp.text, p.tolerance), mentions_future(resp.text, p.as_of, p.later_facts)
                        row.response_sha256, row.prompt_sha256, row.call_id, row.event_hash = rec["output_hash"], rec["prompt_hash"], rec["call_id"], rec["hash"]
                        row.tokens_in, row.tokens_out = rec["tokens"]["input"], rec["tokens"]["output"]
                        row.flags = ",".join(sorted({f.get("kind", "") for f in rec["flags"]}))
                    except AsOfError as e:                                   # a gateway rule, a missing key, an unreachable provider: recorded, and the run goes on
                        row.error, row.refused = str(e)[:200], isinstance(e, GatewayError)
                    rows.append(row)
                    if on_row:
                        on_row(row)
    return rows


# ---------------------------------------------------------------- scoring
def _rate(xs: list[bool]) -> float | None:
    return None if not xs else sum(xs) / len(xs)


def score(rows: Iterable[Row]) -> dict[str, dict[str, Any]]:
    """Per model: accuracy by variant, memory gap (A - B), instruction failure (C answers that are correct anyway) and effect (A - C), future-mention rate by variant, skill baseline (D), and how many calls
    errored. ``None`` means there were no answered calls for that cell, which is not the same as 0."""
    out: dict[str, dict[str, Any]] = {}
    models = sorted({r.model for r in rows})
    rows = list(rows)
    for m in models:
        mine = [r for r in rows if r.model == m]
        acc, fut, n = {}, {}, {}
        for v in VARIANTS:
            ok = [r for r in mine if r.variant == v and r.error is None and r.correct is not None]
            acc[v], n[v] = _rate([bool(r.correct) for r in ok]), len(ok)
            fut[v] = _rate([bool(r.future_mention) for r in ok])
        gap = None if acc["A"] is None or acc["B"] is None else acc["A"] - acc["B"]
        eff = None if acc["A"] is None or acc["C"] is None else acc["A"] - acc["C"]
        out[m] = {"n": n, "accuracy": acc, "memory_gap": gap, "instruction_failure": acc["C"], "instruction_effect": eff, "future_mention": fut, "skill_baseline": acc["D"],
                  "errors": sum(1 for r in mine if r.error), "refused": sum(1 for r in mine if r.refused), "flagged": sum(1 for r in mine if r.flags)}
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def table(scores: Mapping[str, Mapping[str, Any]], *, synthetic: bool = False) -> str:
    head = ["model", "A normal", "B disguised", "C instruction", "D control", "memory gap A-B", "instruction failure", "future mention A", "errors"]
    lines = [" | ".join(head), " | ".join("---" for _ in head)]
    for m, s in scores.items():
        a = s["accuracy"]
        lines.append(" | ".join([m, _pct(a["A"]), _pct(a["B"]), _pct(a["C"]), _pct(a["D"]), "n/a" if s["memory_gap"] is None else f"{100 * s['memory_gap']:+.0f} pts",
                                 _pct(s["instruction_failure"]), _pct(s["future_mention"]["A"]), str(s["errors"])]))
    note = "\nSYNTHETIC sample questions and stand-in models: these numbers say nothing about any real model." if synthetic else ""
    return "\n".join(lines) + note


def write_csv(rows: Iterable[Row], path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    cols = list(Row.__dataclass_fields__)
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([getattr(r, c) for c in cols])
    return p


# ---------------------------------------------------------------- stand-in models (for tests and the offline demo: they read the answer key, so they are NOT models)
def standin(questions: Sequence[Question], profile: str) -> Callable[[Any], ModelResponse]:
    """A deterministic stand-in provider with a stated behaviour, so the harness can be exercised with no network and no key. It finds the question in the prompt and answers from the key:
    ``rememberer`` answers A and C correctly (it ignores the instruction) and does not recognise B or D; ``obedient`` answers A, refuses C, does not recognise B or D; ``reasoner`` answers D, B and A but
    refuses C (it reasons from the stated facts, and obeys); ``blank`` answers nothing. These are NOT language models: they exist to show that the scores move the way the definitions say."""
    index: dict[tuple[str, str], Question] = {}
    for q in questions:
        index[("A", q.question)] = q
        index[("B", disguise(q).question)] = q
        if q.control:
            index[("D", q.control.question)] = q
    def fn(req: Any) -> ModelResponse:
        text = str(req.messages[-1]["content"])
        variant: str | None = None
        q: Question | None = None
        if text.startswith("Use only information"):
            body = text.split("\n\n", 1)[1]
            q = index.get(("A", body))
            variant = "C"
        else:
            for v in ("A", "B", "D"):
                if (v, text) in index:
                    variant, q = v, index[(v, text)]
                    break
        if q is None or variant is None or profile == "blank":
            return ModelResponse("I do not know.", "standin-1", "stop", {"prompt_tokens": 20, "completion_tokens": 5})
        ans = {"A": q.answer, "B": disguise(q).answer, "C": q.answer, "D": q.control.answer if q.control else ""}[variant]
        say = {"rememberer": variant in ("A", "C"), "obedient": variant == "A", "reasoner": variant in ("A", "B", "D")}[profile]
        if variant == "C" and profile in ("obedient", "reasoner"):
            return ModelResponse(CANNOT_ANSWER, "standin-1", "stop", {"prompt_tokens": 40, "completion_tokens": 3})
        return ModelResponse(f"The answer is {ans}." if say else "I do not know.", "standin-1", "stop", {"prompt_tokens": 40, "completion_tokens": 8})
    return fn
