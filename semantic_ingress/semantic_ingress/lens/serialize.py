"""
Compact serving form of a file lens record (what agents read).

One line, about 35% smaller than the stored JSON: line anchors are dropped (they
stay in storage for later build work), symbol names lose their class prefix, and
spaces inside signal details become underscores so tokens stay whole.

    rel3 ORCH+ACT | sig fanout.parallel:thread_pool effect.llm | sym sync:ORCH |
    c X-THREAD-FANOUT X-CHECKPOINT | f E-AO-STC | <narrative>
"""
import re

from semantic_ingress.lens.models import FileLens

_ANCHOR = re.compile(r"@L\d+(-\d+)?$")


def compact_file_lens(lens: FileLens) -> str:
    parts = [f"rel{lens.rel} {'+'.join(lens.kind)}"]
    if lens.sig:
        parts.append("sig " + " ".join(_ANCHOR.sub("", s).replace(" ", "_") for s in lens.sig))
    if lens.sym:
        parts.append("sym " + " ".join(f"{s.n.split('.')[-1]}:{s.k}" for s in lens.sym))
    if lens.c:
        parts.append("c " + " ".join(lens.c))
    if lens.f:
        parts.append("f " + " ".join(dict.fromkeys(x.split("@", 1)[0] for x in lens.f)))
    parts.append(lens.n)
    return " | ".join(parts)
