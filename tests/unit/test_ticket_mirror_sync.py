from html.parser import HTMLParser
from pathlib import Path

from scripts import sync_ticket_mirror as mirror


def test_section_references_support_nested_headings_and_task_list_styles():
    body = """## Child issues

### Wave one

- [ ] #30 — first
1. [x] #31 — second

### Wave two

+ [X] [#32](https://example.test/issues/32) — third
- #33 — fourth
* [#34](https://example.test/issues/34) — fifth

## Source

- [ ] #99 — outside the section
"""

    assert mirror.section_references(body, lambda heading: heading == "child issues") == [
        30,
        31,
        32,
        33,
        34,
    ]


def test_section_references_ignore_fenced_checklist_examples():
    body = """## Child issues

- #30 — real child

```markdown
## Child issues
- [ ] #99 — example only
```

## Source

- #88 — outside the section
"""

    assert mirror.section_references(body, lambda heading: heading == "child issues") == [30]


def test_section_references_only_use_top_level_headings_as_section_boundaries():
    body = """> ## Child issues
> - [ ] #99 — quoted example

## Child issues

- [ ] #30 — real child

> ## Source
> Quoted documentation must not end the real section.

- [ ] #31 — still a real child

## Source

- [ ] #88 — outside the section
"""

    assert mirror.section_references(body, lambda heading: heading == "child issues") == [
        30,
        31,
    ]


class _RenderedHTMLAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def assert_safe_html(rendered: str) -> None:
    audit = _RenderedHTMLAudit()
    audit.feed(rendered)

    forbidden_tags = {"embed", "iframe", "math", "object", "script", "svg"}
    for tag, attrs in audit.tags:
        assert tag not in forbidden_tags
        assert all(not name.lower().startswith("on") for name in attrs)
        for name in {"href", "src"} & attrs.keys():
            destination = (attrs[name] or "").strip().lower()
            assert not destination.startswith(("data:", "javascript:", "vbscript:"))


def test_render_issue_body_preserves_code_and_links_prose_references():
    body = """Use ``#123`` literally and reference #30.

````python
#123
```
#124
````

    #125

#126
"""

    rendered = mirror.render_issue_body(
        body,
        "owner/repository",
        {30, 123, 124, 125, 126},
        heading_offset=2,
        anchor_prefix="issue-500",
    )

    assert "<code>#123</code>" in rendered
    assert '<a href="https://github.com/owner/repository/issues/30">#30</a>' in rendered
    assert "#123\n```\n#124" in rendered
    assert "<pre><code>#125" in rendered
    assert '<a href="https://github.com/owner/repository/issues/126">#126</a>' in rendered
    assert "issues/123" not in rendered
    assert "issues/124" not in rendered
    assert "issues/125" not in rendered
    assert_safe_html(rendered)


def test_render_issue_body_escapes_html_and_rejects_unsafe_links():
    body = """## Unsafe <script>alert(1)</script>

<img src=x onerror=alert(1)>
[bad](javascript:alert(1))
[encoded](jav&#x61;script:alert(1))
![data](data:text/html,<svg onload=alert(1)>)
[safe](https://example.test/path)
"""

    rendered = mirror.render_issue_body(
        body,
        "owner/repository",
        set(),
        heading_offset=2,
        anchor_prefix="issue-500",
    )

    assert "<script" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert 'href="javascript:' not in rendered
    assert 'src="data:' not in rendered
    assert '<a href="https://example.test/path">safe</a>' in rendered
    assert_safe_html(rendered)


def test_render_issue_body_handles_markdown_parser_differential_bypasses():
    body = r"""[code label `bar`](javascript:alert(1))
[escaped \] label](javascript:alert(2))
`safe`\`<img src=x onerror=alert(3)>

lazy paragraph
    <script>alert(4)</script>

[multiline](java
script:alert(5))

   ```lang {onclick=alert(6)}
<svg onload=alert(7)></svg>
   ```

<iframe srcdoc="<script>alert(8)</script>"></iframe>
"""

    rendered = mirror.render_issue_body(
        body,
        "owner/repository",
        set(),
        heading_offset=2,
        anchor_prefix="issue-500",
    )

    assert "&lt;img src=x onerror=alert(3)&gt;" in rendered
    assert "&lt;script&gt;alert(4)&lt;/script&gt;" in rendered
    assert "&lt;svg onload=alert(7)&gt;" in rendered
    assert "&lt;iframe srcdoc=" in rendered
    assert 'href="javascript:' not in rendered
    assert 'onclick="' not in rendered
    assert_safe_html(rendered)


def test_render_issue_body_preserves_gfm_fidelity_and_stable_heading_anchors():
    body = """## Acceptance criteria

- [x] Complete
- [ ] Pending

| State | Count |
| --- | ---: |
| ~~old~~ current | 2 |

https://example.test/evidence
"""

    rendered = mirror.render_issue_body(
        body,
        "owner/repository",
        set(),
        heading_offset=2,
        anchor_prefix="issue-500",
    )

    assert '<h4 id="issue-500-acceptance-criteria">Acceptance criteria</h4>' in rendered
    assert "<table>" in rendered
    assert "<s>old</s> current" in rendered
    assert rendered.count('type="checkbox"') == 2
    assert rendered.count('checked="checked"') == 1
    assert rendered.count('class="task-list-item"') == 2
    assert '<a href="https://example.test/evidence">' in rendered
    assert_safe_html(rendered)


def test_metadata_rendering_is_single_line_and_markdown_safe():
    escaped = mirror.escape_markdown("bad ] | ` value\nnext")

    assert "\n" not in escaped
    assert r"\]" in escaped
    assert r"\|" in escaped
    assert r"\`" in escaped
    assert mirror.code_span("tick`label") == "``tick`label``"


def test_atomic_write_replaces_content_and_preserves_mode(tmp_path: Path):
    output = tmp_path / "tickets.md"
    output.write_text("old\n", encoding="utf-8")
    output.chmod(0o640)

    mirror.write_atomic(output, "new\n")

    assert output.read_text(encoding="utf-8") == "new\n"
    assert output.stat().st_mode & 0o777 == 0o640
