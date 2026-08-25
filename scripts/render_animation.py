"""Render the README animation from a *real* run's event log.

    python scripts/render_animation.py

The point of doing it this way: the animation is not a mock-up of what FORGE
does, it is a playback of what FORGE did. The lifecycle strip is driven by
actual `PHASE_ENTERED` events, the log pane shows actual appended events with
their real sequence numbers, and the counters are read off the real
`RunResult`. If the runtime changes, re-running this changes the GIF - the
animation cannot drift away from the truth.

The scenario is the one that matters: run, kill the worker mid-flight, restart,
resume from checkpoint, and show the already-completed effects being reused
rather than repeated.

Output: docs/assets/forge-demo.gif
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forge.core.contracts import TaskSpec  # noqa: E402
from forge.core.enums import EventType  # noqa: E402
from forge.core.events import Event  # noqa: E402
from forge.evaluation.faults import FaultClass, FaultInjector  # noqa: E402
from forge.llm.gateway import CostLedger, LLMGateway  # noqa: E402
from forge.llm.mock import MockProvider  # noqa: E402
from forge.runtime.loop import AgentRuntime, RuntimeConfig, SimulatedCrash  # noqa: E402
from forge.security.policy import PolicyBundle, PolicyEngine  # noqa: E402
from forge.state.sqlite_store import SQLiteEventStore  # noqa: E402
from forge.tools.builtin import build_default_registry  # noqa: E402

# ─────────────────────────────────────────────────────────────── design system

W, H = 1000, 560

BG        = (11, 19, 18)
PANEL     = (17, 28, 26)
PANEL_2   = (21, 34, 31)
LINE      = (34, 50, 47)
LINE_2    = (46, 65, 62)
INK       = (228, 237, 234)
INK_2     = (147, 173, 169)
INK_3     = (100, 117, 111)
ACCENT    = (79, 207, 187)      # verdigris - the runtime working
ACCENT_DK = (18, 49, 44)
EMBER     = (224, 133, 99)      # policy refusal / risk
CRIMSON   = (229, 72, 77)       # the crash
GOLD      = (229, 192, 123)     # idempotency reuse - the payoff colour

PHASES = [
    "BOOT", "VIEW", "PROPOSE", "VALIDATE", "AUTHORIZE",
    "DISPATCH", "OBSERVE", "RECONCILE", "COMMIT", "EVALUATE",
]

# Event types worth a line in the log pane. Phase transitions drive the strip
# instead, and context-compilation noise would crowd out the decisions.
NOTABLE = {
    EventType.RUN_CREATED, EventType.RUN_RESUMED, EventType.MODEL_CALLED,
    EventType.PROPOSAL_RECEIVED, EventType.POLICY_DECIDED, EventType.PERMIT_ISSUED,
    EventType.ACTION_DISPATCHED, EventType.EFFECT_OBSERVED, EventType.EFFECT_REUSED,
    EventType.EFFECT_RECONCILED, EventType.STEP_COMMITTED, EventType.CHECKPOINT_WRITTEN,
    EventType.RUN_COMPLETED,
}

EVENT_COLOUR: dict[EventType, tuple[int, int, int]] = {
    EventType.RUN_CREATED: ACCENT,
    EventType.RUN_RESUMED: ACCENT,
    EventType.MODEL_CALLED: INK_2,
    EventType.PROPOSAL_RECEIVED: INK,
    EventType.PERMIT_ISSUED: ACCENT,
    EventType.ACTION_DISPATCHED: INK,
    EventType.EFFECT_OBSERVED: ACCENT,
    EventType.EFFECT_REUSED: GOLD,
    EventType.EFFECT_RECONCILED: INK_2,
    EventType.STEP_COMMITTED: ACCENT,
    EventType.CHECKPOINT_WRITTEN: ACCENT,
    EventType.RUN_COMPLETED: ACCENT,
}

FONT_DIR = Path("C:/Windows/Fonts")


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for name in names:
        path = FONT_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    for name in names:  # try the system resolver before giving up
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


MONO   = ["consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc"]
MONO_B = ["consolab.ttf", "DejaVuSansMono-Bold.ttf"]
UI     = ["segoeui.ttf", "DejaVuSans.ttf", "Helvetica.ttc"]
UI_B   = ["segoeuib.ttf", "DejaVuSans-Bold.ttf"]

F_TITLE  = _font(UI_B, 21)
F_SUB    = _font(UI, 13)
F_LABEL  = _font(MONO_B, 10)
F_CHIP   = _font(MONO_B, 11)
F_LOG    = _font(MONO, 12)
F_BODY   = _font(MONO, 14)
F_BODY_B = _font(MONO_B, 14)
F_BIG    = _font(MONO_B, 26)
F_NUM    = _font(MONO_B, 20)
F_BANNER = _font(UI_B, 17)


# ────────────────────────────────────────────────────────────────── scene state


@dataclass
class LogRow:
    seq: int
    label: str
    detail: str
    colour: tuple[int, int, int]
    age: int = 0
    """Frames since the row appeared. Drives the fade-in highlight."""


@dataclass
class Scene:
    run_id: str = ""
    worker: str = "worker-1"
    active_phase: str | None = None
    reached: set[str] = field(default_factory=set)
    step_no: int = 0
    proposal: str = ""
    policy: str = ""
    policy_colour: tuple[int, int, int] = INK_2
    effect: str = ""
    effect_colour: tuple[int, int, int] = INK_2
    rows: list[LogRow] = field(default_factory=list)
    committed: list[int] = field(default_factory=list)
    checkpoints: list[int] = field(default_factory=list)
    banner: tuple[str, str, str, tuple[int, int, int]] | None = None
    """(kicker, title, subtitle, colour) - drawn as a modal over a dimmed panel."""
    dead: bool = False
    counters: dict[str, Any] = field(default_factory=lambda: {
        "performed": 0, "reused": 0, "duplicates": 0, "usd": 0.0,
    })
    caption: str = ""

    def copy(self) -> Scene:
        return Scene(
            run_id=self.run_id, worker=self.worker, active_phase=self.active_phase,
            reached=set(self.reached), step_no=self.step_no, proposal=self.proposal,
            policy=self.policy, policy_colour=self.policy_colour, effect=self.effect,
            effect_colour=self.effect_colour,
            rows=[LogRow(r.seq, r.label, r.detail, r.colour, r.age) for r in self.rows],
            committed=list(self.committed), checkpoints=list(self.checkpoints),
            banner=self.banner, dead=self.dead, counters=dict(self.counters),
            caption=self.caption,
        )


# ─────────────────────────────────────────────────────────────────── primitives


def panel(d: ImageDraw.ImageDraw, box, *, fill=PANEL, outline=LINE, r=6) -> None:
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=1)


def label(d: ImageDraw.ImageDraw, xy, text: str, colour=INK_3) -> None:
    d.text(xy, text.upper(), font=F_LABEL, fill=colour)


def mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# ──────────────────────────────────────────────────────────────────── rendering


def render(s: Scene) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    _header(d, s)
    _lifecycle(d, s)
    _step_card(d, s)
    _timeline(d, s)
    _log(d, s)
    _counters(d, s)

    if s.banner:
        # Dim the whole panel and present the message as a modal. A bar laid
        # over the live panels clips them and reads like an accident; dimming
        # says "stop and read this" and keeps the state legible underneath.
        img = Image.blend(img, Image.new("RGB", (W, H), BG), 0.66)
        _banner(ImageDraw.Draw(img), s)
    return img


def _header(d: ImageDraw.ImageDraw, s: Scene) -> None:
    d.text((28, 20), "FORGE", font=F_TITLE, fill=INK)
    d.text((100, 26), "durable agent runtime", font=F_SUB, fill=INK_3)

    # worker identity - turns crimson when the process dies
    wcol = CRIMSON if s.dead else ACCENT
    d.ellipse((W - 372, 29, W - 366, 35), fill=wcol)
    d.text((W - 358, 24), s.worker, font=F_LOG, fill=wcol)

    d.text((W - 268, 24), s.run_id, font=F_LOG, fill=INK_3)
    d.text((W - 92, 24), "$0.0000", font=F_LOG, fill=ACCENT)
    d.line((0, 52, W, 52), fill=LINE, width=1)


def _lifecycle(d: ImageDraw.ImageDraw, s: Scene) -> None:
    top, h = 66, 30
    pad, gap = 28, 6
    total = W - pad * 2
    cw = (total - gap * (len(PHASES) - 1)) / len(PHASES)

    label(d, (pad, top - 13), "lifecycle", INK_3)

    for i, name in enumerate(PHASES):
        x0 = pad + i * (cw + gap)
        box = (x0, top, x0 + cw, top + h)
        active = name == s.active_phase
        done = name in s.reached and not active

        if active:
            fill, outline, tcol = ACCENT_DK, ACCENT, ACCENT
        elif done:
            fill, outline, tcol = PANEL, LINE_2, INK_2
        else:
            fill, outline, tcol = BG, LINE, INK_3

        d.rounded_rectangle(box, radius=4, fill=fill, outline=outline, width=1)
        tw = d.textlength(name, font=F_CHIP)
        d.text((x0 + (cw - tw) / 2, top + 9), name, font=F_CHIP, fill=tcol)

        if active:  # underline the live phase
            d.line((x0 + 6, top + h + 3, x0 + cw - 6, top + h + 3), fill=ACCENT, width=2)

        if i < len(PHASES) - 1:
            cx = x0 + cw + gap / 2
            d.line((cx - 1, top + h / 2, cx + 1, top + h / 2), fill=LINE_2, width=1)


def _step_card(d: ImageDraw.ImageDraw, s: Scene) -> None:
    box = (28, 128, 560, 300)
    panel(d, box)
    label(d, (44, 142), "current step", INK_3)
    if s.step_no:
        d.text((box[2] - 74, 138), f"step {s.step_no}", font=F_LOG, fill=INK_2)

    rows = [
        ("model proposes", s.proposal, INK, "The model's proposal - not yet an action."),
        ("runtime authorizes", s.policy, s.policy_colour, ""),
        ("effect observed", s.effect, s.effect_colour, ""),
    ]
    y = 166
    for name, value, colour, _ in rows:
        label(d, (44, y), name, INK_3)
        d.text((44, y + 15), ellipsize(value or "\u2014", 58), font=F_BODY, fill=colour)
        y += 44
        if y < 290:
            d.line((44, y - 12, box[2] - 16, y - 12), fill=LINE, width=1)


def _timeline(d: ImageDraw.ImageDraw, s: Scene) -> None:
    box = (28, 312, 560, 396)
    panel(d, box)
    label(d, (44, 326), "steps committed \u00b7 checkpoints", INK_3)

    x0, y = 52, 364
    slots = 6
    span = (box[2] - 40 - x0) / slots

    for i in range(slots):
        cx = x0 + i * span + span / 2
        n = i + 1
        done = n in s.committed
        colour = ACCENT if done else LINE_2

        if i:
            prev = x0 + (i - 1) * span + span / 2
            d.line((prev + 13, y, cx - 13, y), fill=ACCENT if done else LINE, width=2)

        d.ellipse((cx - 11, y - 11, cx + 11, y + 11),
                  fill=ACCENT_DK if done else BG, outline=colour, width=2)
        tw = d.textlength(str(n), font=F_CHIP)
        d.text((cx - tw / 2, y - 6), str(n), font=F_CHIP, fill=colour if done else INK_3)

        if n in s.checkpoints:  # checkpoint marker under the step
            d.polygon([(cx - 5, y + 18), (cx + 5, y + 18), (cx, y + 26)], fill=ACCENT)
            d.text((cx - 14, y + 28), "ckpt", font=F_LABEL, fill=ACCENT)


def _log(d: ImageDraw.ImageDraw, s: Scene) -> None:
    box = (576, 128, W - 28, 396)
    panel(d, box)
    label(d, (592, 142), "append-only event log", INK_3)
    d.text((box[2] - 66, 140), "durable", font=F_LABEL, fill=ACCENT)
    d.line((592, 158, box[2] - 16, 158), fill=LINE, width=1)

    visible = s.rows[-13:]
    y = 166
    for row in visible:
        fresh = max(0.0, 1.0 - row.age / 5.0)
        if fresh > 0:  # brief wash as the row lands
            d.rounded_rectangle((588, y - 2, box[2] - 16, y + 15), radius=3,
                                fill=mix(PANEL, PANEL_2, fresh))

        d.text((592, y), f"{row.seq:>3}", font=F_LOG, fill=INK_3)
        colour = mix(row.colour, (255, 255, 255), fresh * 0.35)
        d.text((624, y), row.label, font=F_LOG, fill=colour)
        if row.detail:
            x = 624 + d.textlength(row.label + " ", font=F_LOG)
            d.text((x, y), ellipsize(row.detail, 22), font=F_LOG, fill=INK_3)
        y += 17


def _counters(d: ImageDraw.ImageDraw, s: Scene) -> None:
    box = (28, 408, W - 28, 486)
    panel(d, box, fill=PANEL)

    c = s.counters
    cells = [
        ("effects performed", str(c["performed"]), ACCENT),
        ("effects reused", str(c["reused"]), GOLD if c["reused"] else INK_2),
        ("duplicate effects", str(c["duplicates"]),
         ACCENT if c["duplicates"] == 0 else CRIMSON),
        ("total cost", f"${c['usd']:.4f}", ACCENT),
    ]
    cw = (box[2] - box[0]) / len(cells)
    for i, (name, value, colour) in enumerate(cells):
        x = box[0] + i * cw + 22
        label(d, (x, 424), name, INK_3)
        d.text((x, 442), value, font=F_NUM, fill=colour)
        if i:
            lx = box[0] + i * cw
            d.line((lx, 420, lx, 474), fill=LINE, width=1)

    if s.caption:
        d.text((28, 504), s.caption, font=F_SUB, fill=INK_2)


def _banner(d: ImageDraw.ImageDraw, s: Scene) -> None:
    assert s.banner
    kicker, title, sub, colour = s.banner

    bw, bh = 660, 148
    x0, y0 = (W - bw) / 2, (H - bh) / 2 - 16
    box = (x0, y0, x0 + bw, y0 + bh)
    d.rounded_rectangle(box, radius=8, fill=PANEL, outline=colour, width=2)
    d.line((x0 + 2, y0 + 2, x0 + bw - 2, y0 + 2), fill=colour, width=3)

    kw = d.textlength(kicker.upper(), font=F_LABEL)
    d.text(((W - kw) / 2, y0 + 26), kicker.upper(), font=F_LABEL, fill=colour)

    tw = d.textlength(title, font=F_BANNER)
    d.text(((W - tw) / 2, y0 + 52), title, font=F_BANNER, fill=colour)

    sw = d.textlength(sub, font=F_SUB)
    d.text(((W - sw) / 2, y0 + 90), sub, font=F_SUB, fill=INK_2)


# ─────────────────────────────────────────────────────────────────── storyboard


def capture() -> dict[str, Any]:
    """Execute the crash/resume scenario and return its real event log."""
    db = ROOT / ".forge" / "animation.db"
    if db.exists():
        db.unlink()

    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "search_corpus",
                      "arguments": {"query": "checkpointing"}}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "read_document",
                      "arguments": {"key": "checkpointing"}}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                      "arguments": {"name": "finding", "content": "recovers 100%"}}},
        {"proposal": {"kind": "ANSWER",
                      "answer": "Per-step checkpointing recovered 100% of interrupted runs."}},
    ]
    tools = ["search_corpus", "read_document", "calculate", "save_note"]

    def build(faults: Any = None) -> AgentRuntime:
        return AgentRuntime(
            store=store,
            gateway=LLMGateway(providers=[MockProvider(script)], ledger=CostLedger()),
            registry=build_default_registry(),
            policy=PolicyEngine(
                PolicyBundle.zero_cost(granted=["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"])
            ),
            config=RuntimeConfig(max_steps=10),
            faults=faults,
        )

    async def main() -> dict[str, Any]:
        await store.open()
        run_id = ""
        try:
            await build(FaultInjector.single(FaultClass.WORKER_CRASH, at_step=3)).start(
                TaskSpec(goal="What did FORGE measure about checkpointing?", tools=tools)
            )
        except SimulatedCrash:
            run_id = str((await store.list_runs(limit=1))[0]["run_id"])

        before = await store.read(run_id)
        crash_seq = before[-1].seq
        result = await build().resume(run_id)
        events = await store.read(run_id)
        await store.close()
        return {"events": events, "crash_seq": crash_seq, "run_id": run_id, "result": result}

    store = SQLiteEventStore(db)
    return asyncio.run(main())


def _row_for(ev: Event) -> LogRow | None:
    """Turn an event into a log line, or None if it is not worth showing."""
    if ev.type not in NOTABLE:
        return None
    p = ev.payload
    detail = ""
    colour = EVENT_COLOUR.get(ev.type, INK_2)

    match ev.type:
        case EventType.PROPOSAL_RECEIVED:
            detail = str(p.get("tool") or "answer")
        case EventType.POLICY_DECIDED:
            decision = str(p.get("decision"))
            detail = decision
            colour = ACCENT if decision == "ALLOW" else EMBER
        case EventType.ACTION_DISPATCHED | EventType.STEP_COMMITTED:
            detail = str(p.get("tool") or "")
        case EventType.EFFECT_OBSERVED:
            detail = "ok" if p.get("ok") else "failed"
            colour = ACCENT if p.get("ok") else EMBER
        case EventType.EFFECT_REUSED:
            detail = "no duplicate"
        case EventType.CHECKPOINT_WRITTEN:
            detail = f"seq {p.get('last_seq')}"
        case EventType.MODEL_CALLED:
            detail = str(p.get("provider") or "")
        case _:
            detail = ""

    return LogRow(seq=ev.seq, label=ev.type.value, detail=detail, colour=colour)


def storyboard(data: dict[str, Any]) -> list[tuple[Scene, int]]:
    """Walk the real event log, emitting (scene, hold-frames) pairs."""
    events: list[Event] = data["events"]
    crash_seq: int = data["crash_seq"]
    result = data["result"]

    s = Scene(run_id=data["run_id"])
    out: list[tuple[Scene, int]] = []

    def emit(hold: int = 1) -> None:
        for row in s.rows:
            row.age += 1
        out.append((s.copy(), hold))

    s.caption = "A run executes. Every phase, decision and effect is appended to a durable log."
    emit(10)

    crashed = False
    for ev in events:
        if not crashed and ev.seq > crash_seq:
            # ── the worker dies ──────────────────────────────────────────
            crashed = True
            s.dead = True
            s.active_phase = None
            s.banner = (
                "process terminated",
                "WORKER KILLED  \u00b7  SIGKILL mid-dispatch",
                "The dispatch was logged. The effect never was.",
                CRIMSON,
            )
            s.caption = "The process dies with no chance to clean up. Nothing is written."
            emit(17)

            s.banner = (
                "recovery",
                "NEW WORKER  \u00b7  resuming from the last checkpoint",
                "Canonical state is restored by folding the durable event log.",
                ACCENT,
            )
            s.worker = "worker-2"
            s.dead = False
            s.caption = (
                f"A fresh worker restores canonical state from checkpoint "
                f"at step {max(s.checkpoints) if s.checkpoints else 0}."
            )
            emit(17)
            s.banner = None
            s.reached.clear()
            emit(2)

        if ev.type is EventType.PHASE_ENTERED:
            phase = str(ev.payload.get("phase"))
            if phase in PHASES:
                s.active_phase = phase
                s.reached.add(phase)
                emit(1)
            continue

        if ev.type is EventType.STEP_STARTED:
            s.step_no = int(ev.payload.get("index", s.step_no))
            s.proposal = s.policy = s.effect = ""
            s.policy_colour = s.effect_colour = INK_2
            s.reached.clear()
            continue

        row = _row_for(ev)

        match ev.type:
            case EventType.PROPOSAL_RECEIVED:
                tool = ev.payload.get("tool")
                if tool:
                    args = ev.payload.get("arguments") or {}
                    inner = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    s.proposal = f"{tool}({ellipsize(inner, 30)})"
                else:
                    s.proposal = "ANSWER \u2014 enough evidence gathered"
            case EventType.POLICY_DECIDED:
                decision = str(ev.payload.get("decision"))
                cap = ev.payload.get("capability") or "\u2014"
                s.policy = f"{decision}  \u00b7  {cap}"
                s.policy_colour = ACCENT if decision == "ALLOW" else EMBER
            case EventType.EFFECT_OBSERVED:
                ok = bool(ev.payload.get("ok"))
                s.effect = ("performed  \u00b7  recorded under its idempotency key"
                            if ok else "failed  \u00b7  classified and reconciled")
                s.effect_colour = ACCENT if ok else EMBER
                s.counters["performed"] += 1
            case EventType.EFFECT_REUSED:
                s.effect = "REUSED  \u00b7  already done \u2014 dispatch suppressed"
                s.effect_colour = GOLD
                s.counters["reused"] += 1
                s.caption = ("The model re-proposed work the dead worker had already "
                             "finished. The idempotency key suppressed it.")
            case EventType.STEP_COMMITTED:
                if s.step_no and s.step_no not in s.committed:
                    s.committed.append(s.step_no)
            case EventType.CHECKPOINT_WRITTEN:
                if s.step_no and s.step_no not in s.checkpoints:
                    s.checkpoints.append(s.step_no)
            case _:
                pass

        if row is not None:
            s.rows.append(row)
            hold = 4 if ev.type is EventType.EFFECT_REUSED else 2
            emit(hold)

    # ── closing card ────────────────────────────────────────────────────
    s.active_phase = None
    s.counters["duplicates"] = result.duplicate_effects
    s.counters["usd"] = result.usage.usd
    s.banner = (
        "run complete",
        "0 duplicate effects  \u00b7  $0.0000",
        "Crashed mid-write, resumed, finished. Nothing happened twice.",
        ACCENT,
    )
    s.caption = "Crashed, resumed, finished. No external effect happened twice."
    emit(34)
    return out


# ──────────────────────────────────────────────────────────────────────── build


def _shared_palette(frames: list[Image.Image]) -> Image.Image:
    """One palette for the whole animation, built from a full colour census.

    Every frame shares a palette, because quantising frames independently
    makes colours shimmer between them and inflates the file. Choosing *which*
    colours matters more than it looks: the two most meaningful colours here -
    crimson for the crash, gold for a reused effect - each occupy only a
    couple of frames out of a hundred, so any scheme that samples frames will
    step straight over them and silently map them to the nearest grey.

    So census every colour, then median-cut over the distinct set with each
    colour weighted once. A colour that appears in two frames then gets the
    same say in the palette as the background, which is precisely the
    behaviour the payoff shots need.
    """
    from collections import Counter

    census: Counter[tuple[int, int, int]] = Counter()
    for frame in frames:
        for count, colour in frame.getcolors(maxcolors=1 << 24) or []:
            census[colour] += count

    colours = [c for c, _ in census.most_common(8000)]
    strip = Image.new("RGB", (len(colours), 1))
    strip.putdata(colours)
    return strip.quantize(colors=200, method=Image.MEDIANCUT)


def build_gif(beats: list[tuple[Scene, int]], out: Path, *, ms: int = 60) -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for scene, hold in beats:
        frames.append(render(scene))
        durations.append(ms * hold)

    base = _shared_palette(frames)
    quantised = [f.quantize(palette=base, dither=Image.Dither.NONE) for f in frames]

    out.parent.mkdir(parents=True, exist_ok=True)
    quantised[0].save(
        out,
        save_all=True,
        append_images=quantised[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )


def main() -> int:
    print("capturing a real run (crash + resume) ...")
    data = capture()
    result = data["result"]
    print(f"  run {data['run_id']}  events={len(data['events'])}  "
          f"status={result.status.value}  duplicates={result.duplicate_effects}")

    if result.duplicate_effects:  # the animation asserts this is zero
        raise SystemExit(f"refusing to render: {result.duplicate_effects} duplicate effects")

    beats = storyboard(data)
    total_ms = sum(hold for _, hold in beats) * 60
    print(f"  {len(beats)} frames, {total_ms / 1000:.1f}s")

    out = ROOT / "docs" / "assets" / "forge-demo.gif"
    build_gif(beats, out)
    size_mb = out.stat().st_size / 1_048_576
    print(f"  wrote {out.relative_to(ROOT)}  ({size_mb:.2f} MB)")

    # A still for social previews and anywhere the GIF will not animate.
    # The most informative frame is the last one with no modal covering the
    # panel but the reuse counters already showing.
    poster = next(
        (s for s, _ in reversed(beats) if s.banner is None and s.counters["reused"]),
        beats[-1][0],
    )
    poster_path = out.with_name("forge-panel.png")
    render(poster).save(poster_path)
    print(f"  wrote {poster_path.relative_to(ROOT)}")

    shutil.rmtree(ROOT / ".forge" / "animation.db", ignore_errors=True)
    (ROOT / ".forge" / "animation.db").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
