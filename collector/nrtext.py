"""National Rail Knowledgebase incident text helpers (stdlib only), shared by collect.py and aggregate.py.

Turns an incident's HTML Description / RoutesAffected into readable text so the reason behind a
National Rail status is stored and shown as words, not just a link.
"""
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "tr", "table", "section"}
_HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.kind = [], [], "p"

    def flush(self):
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if t:
            self.out.append({self.kind: t})
        self.buf, self.kind = [], "p"

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK or tag in _HEAD or tag == "li":
            self.flush()
            self.kind = "h" if tag in _HEAD else ("li" if tag == "li" else "p")

    def handle_endtag(self, tag):
        if tag in _BLOCK or tag in _HEAD or tag == "li":
            self.flush()

    def handle_data(self, data):
        self.buf.append(data)


def html_blocks(s, cap=6000):
    """HTML -> [{"p"|"h"|"li": text}], links flattened to their words, total text capped at `cap` chars."""
    if not s:
        return []
    p = _Blocks()
    try:
        p.feed(s)
        p.close()
    except Exception:
        return [{"p": re.sub(r"<[^>]+>", " ", s).strip()[:cap]}]
    p.flush()
    out, n = [], 0
    for b in p.out:
        (k, t), = b.items()
        if n + len(t) > cap:
            room = max(0, cap - n)
            if room > 20:
                out.append({k: t[:room].rsplit(" ", 1)[0] + "…"})
            elif out:
                (k2, t2), = out[-1].items()
                out[-1] = {k2: t2.rstrip(".") + "…"}
            break
        out.append(b); n += len(t)
    return out


def blocks_text(blocks):
    """Blocks -> plain text: one block per line, list items bulleted."""
    return "\n".join(("• " + t) if k == "li" else t for b in blocks for k, t in b.items())


def html_text(s, cap=6000):
    return blocks_text(html_blocks(s, cap)) or None


def norm_url(u):
    return (u or "").strip().rstrip("/").lower()
