"""Optional, offline page-title extraction; usable as a module or a small CLI."""
from __future__ import annotations

from html.parser import HTMLParser


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.capture = None
        self.headings = []
        self.page_title = ""
        self.og_title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property", "").lower() == "og:title":
            self.og_title = self.og_title or attrs.get("content", "")
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"}:
            if self.capture is not None:
                self.capture[2].append(" ")
            return
        excluded = (any(hidden for _, hidden in self.stack)
                    or tag in {"nav", "footer", "aside", "script", "style"}
                    or "hidden" in attrs or attrs.get("aria-hidden") == "true")
        self.stack.append((tag, excluded))
        if tag in {"h1", "title"} and not excluded and self.capture is None:
            in_content = any(parent in {"main", "article"} for parent, _ in self.stack)
            self.capture = [tag, in_content, []]

    def handle_endtag(self, tag):
        if self.capture is not None and self.capture[0] == tag:
            kind, in_content, parts = self.capture
            value = " ".join("".join(parts).split())
            if kind == "h1" and value:
                self.headings.append((not in_content, value))
            elif kind == "title":
                self.page_title = value
            self.capture = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.capture is not None and not any(hidden for _, hidden in self.stack):
            self.capture[2].append(data)


def extract_page_title(html: str) -> tuple[str, str] | None:
    """Return (original headline, source), without network calls or case rewriting.

    Prefer an article/main H1, then another visible H1, Open Graph, and HTML
    title. Return None when there is no page title; callers retain their input.
    """
    parser = _TitleParser()
    parser.feed(html)
    parser.close()
    if parser.headings:
        return min(parser.headings, key=lambda item: item[0])[1], "h1"
    for value, source in ((parser.og_title, "og:title"), (parser.page_title, "title")):
        value = " ".join(value.split())
        if value:
            return value, source
    return None


def main() -> None:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path, help="Saved UTF-8 HTML file; no URL fetching")
    args = parser.parse_args()
    result = extract_page_title(args.html.read_text(encoding="utf-8"))
    print(json.dumps({"title": result[0], "source": result[1]} if result else None,
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
