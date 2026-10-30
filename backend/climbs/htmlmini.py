"""Tiny DOM built on html.parser - enough for targeted scraping without bs4/lxml."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_WS = re.compile(r"\s+")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or {}), parent
        self.children: list = []
        self.text_ = None

    # -- traversal -------------------------------------------------------
    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find_all(self, tag=None, cls=None, id=None, attr=None, pred=None):
        out = []
        for n in self.iter():
            if n is self:
                continue
            if tag and n.tag != tag:
                continue
            if cls and cls not in n.classes:
                continue
            if id and n.attrs.get("id") != id:
                continue
            if attr and not all(n.attrs.get(k) == v if v is not None else k in n.attrs for k, v in attr.items()):
                continue
            if pred and not pred(n):
                continue
            out.append(n)
        return out

    def find(self, *a, **k):
        r = self.find_all(*a, **k)
        return r[0] if r else None

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def get(self, k, default=None):
        return self.attrs.get(k, default)

    # -- text ------------------------------------------------------------
    def text(self, sep=" "):
        parts = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag in ("script", "style"):
                continue
            elif c.tag == "br":
                parts.append("\n")
            else:
                t = c.text(sep)
                if t:
                    parts.append(t)
                if c.tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"):
                    parts.append("\n")
        return _WS.sub(" ", unescape("".join(parts))).strip()

    def text_lines(self):
        """Text with block boundaries preserved as newlines."""
        parts = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag in ("script", "style"):
                continue
            elif c.tag == "br":
                parts.append("\n")
            else:
                parts.append(c.text_lines())
                if c.tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"):
                    parts.append("\n")
        s = unescape("".join(parts))
        return "\n".join(l.strip() for l in re.sub(r"[ \t]+", " ", s).split("\n") if l.strip())

    def next_sibling_tag(self):
        if not self.parent:
            return None
        sib = self.parent.children
        i = sib.index(self)
        for c in sib[i + 1:]:
            if isinstance(c, Node):
                return c
        return None

    def __repr__(self):
        return f"<{self.tag} {self.attrs}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node("document")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)

    def handle_entityref(self, name):
        self.cur.children.append(f"&{name};")

    def handle_charref(self, name):
        self.cur.children.append(f"&#{name};")


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def meta(doc: Node, prop: str) -> str | None:
    for m in doc.find_all("meta"):
        if m.get("property") == prop or m.get("name") == prop:
            return unescape(m.get("content") or "") or None
    return None
