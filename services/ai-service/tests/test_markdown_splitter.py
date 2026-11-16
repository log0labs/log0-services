"""Step 5.2: splitting a Markdown runbook into chunks that keep their heading context."""

from itertools import pairwise

import pytest

from app.retrieval.splitter import Chunk, split_markdown

RUNBOOK = """\
Intro text before any heading.

# Database

General notes.

## Connection pool

Check the pool size.

```sh
# not a heading, a shell comment

psql -c "select 1"
```

## Slow queries

Run EXPLAIN.

# Cache

Flush it.
"""


def test_cuts_at_headings_and_keeps_the_heading_path():
    chunks = split_markdown(RUNBOOK)
    assert [(c.heading_path, c.content.split("\n")[0]) for c in chunks] == [
        ("", "Intro text before any heading."),
        ("Database", "General notes."),
        ("Database > Connection pool", "Check the pool size."),
        ("Database > Slow queries", "Run EXPLAIN."),
        ("Cache", "Flush it."),
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3, 4]


def test_a_heading_inside_a_code_fence_is_not_a_heading():
    pool = split_markdown(RUNBOOK)[2]
    assert "# not a heading, a shell comment" in pool.content
    assert all("not a heading" not in c.heading_path for c in split_markdown(RUNBOOK))


def test_a_heading_with_no_text_still_names_its_children():
    chunks = split_markdown("# Top\n\n## Child\n\ntext\n")
    assert [(c.heading_path, c.content) for c in chunks] == [("Top > Child", "text")]


def test_sibling_headings_replace_each_other_in_the_path():
    chunks = split_markdown("# A\n## B\nb text\n## C\nc text\n# D\n## E\ne text\n")
    assert [c.heading_path for c in chunks] == ["A > B", "A > C", "D > E"]


def test_embed_text_puts_the_heading_path_in_front():
    chunk = Chunk(ordinal=0, heading_path="Database > Pool", content="Check the size.")
    assert chunk.embed_text == "Database > Pool\nCheck the size."
    assert Chunk(ordinal=0, heading_path="", content="x").embed_text == "x"


def test_a_long_section_is_packed_from_whole_paragraphs():
    paragraphs = [f"paragraph {i} " + "word " * 30 for i in range(10)]  # ~165 chars each
    chunks = split_markdown("# T\n\n" + "\n\n".join(paragraphs), max_chars=500, overlap=50)
    assert len(chunks) > 1
    assert all(len(c.content) <= 500 for c in chunks)
    assert all(c.heading_path == "T" for c in chunks)
    joined = "\n\n".join(c.content for c in chunks)
    for p in paragraphs:
        assert p.strip() in joined  # no paragraph was cut


def test_a_fenced_block_is_not_cut_at_a_blank_line_inside_it():
    code = "```\n" + "\n\n".join(f"line {i}" for i in range(6)) + "\n```"
    text = "# T\n\n" + "intro " * 60 + "\n\n" + code
    chunks = split_markdown(text, max_chars=400, overlap=50)
    holder = [c for c in chunks if "line 0" in c.content]
    assert len(holder) == 1
    assert all(f"line {i}" in holder[0].content for i in range(6))


def test_one_huge_paragraph_is_cut_into_overlapping_windows():
    text = "# T\n\n" + "".join(f"{i % 10}" for i in range(1000))  # one 1000-char block, no blank lines
    chunks = split_markdown(text, max_chars=300, overlap=60)
    assert len(chunks) == 4
    assert all(len(c.content) <= 300 for c in chunks)
    for a, b in pairwise(chunks):
        assert a.content[-60:] == b.content[:60]  # the overlap
    assert chunks[-1].content.endswith(text[-20:])  # the end of the text is not lost


def test_empty_and_blank_input_gives_no_chunks():
    assert split_markdown("") == []
    assert split_markdown("   \n\n\t\n") == []
    assert split_markdown("# Only a heading\n") == []


def test_windows_line_endings_are_handled():
    chunks = split_markdown("# A\r\n\r\ntext one\r\n\r\n## B\r\n\r\ntext two\r\n")
    assert [(c.heading_path, c.content) for c in chunks] == [("A", "text one"), ("A > B", "text two")]


def test_heading_levels_beyond_four_and_missing_space_are_plain_text():
    chunks = split_markdown("# A\n\n##### five\n#nospace\ntext\n")
    assert len(chunks) == 1
    assert "##### five" in chunks[0].content and "#nospace" in chunks[0].content


def test_closing_hashes_are_dropped_from_the_title():
    assert split_markdown("# Title ##\n\nbody\n")[0].heading_path == "Title"


@pytest.mark.parametrize(("max_chars", "overlap"), [(50, 0), (200, 100), (200, -1)])
def test_bad_limits_are_refused(max_chars, overlap):
    with pytest.raises(ValueError):
        split_markdown("text", max_chars=max_chars, overlap=overlap)


def test_nothing_is_lost_for_text_that_fits():
    text = "# A\n\nshort one.\n\n## B\n\nshort two.\n"
    assert "".join(c.content for c in split_markdown(text)) == "short one.short two."
