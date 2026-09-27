from app.domain.chunker import (
    chunk_text,
    count_words,
    split_paragraphs,
    split_sentences,
    strip_html,
    word_count_from_html,
)


def test_strip_html_removes_tags_and_scripts():
    raw = "<p>\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627</p><script>var x = 1;</script><style>body{}</style><div>\u062f\u0648\u0645</div>"
    text = strip_html(raw)
    assert "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627" in text
    assert "var x" not in text
    assert "\u062f\u0648\u0645" in text
    assert "<" not in text


def test_strip_html_unescapes_entities():
    assert strip_html("<p>a &amp; b</p>") == "a & b"


def test_paragraph_splitting():
    text = "\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0627\u0648\u0644.\n\n\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u062f\u0648\u0645.\n\n\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0633\u0648\u0645."
    paras = split_paragraphs(text)
    assert len(paras) == 3
    assert paras[0] == "\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0627\u0648\u0644."


def test_persian_sentence_splitting():
    text = "\u062c\u0645\u0644\u0647 \u0627\u0648\u0644. \u062c\u0645\u0644\u0647 \u062f\u0648\u0645\u061f \u062c\u0645\u0644\u0647 \u0633\u0648\u0645! \u062c\u0645\u0644\u0647 \u0686\u0647\u0627\u0631\u0645\u061b \u062c\u0645\u0644\u0647 \u067e\u0646\u062c\u0645\n\u062c\u0645\u0644\u0647 \u0634\u0634\u0645"
    sentences = split_sentences(text)
    assert len(sentences) == 6
    assert sentences[0] == "\u062c\u0645\u0644\u0647 \u0627\u0648\u0644."
    assert sentences[1] == "\u062c\u0645\u0644\u0647 \u062f\u0648\u0645\u061f"
    assert "\u062c\u0645\u0644\u0647 \u0634\u0634\u0645" in sentences[-1]


def test_chunk_size_and_overlap_word_strategy():
    text = " ".join(f"word{i}" for i in range(20))
    chunks = chunk_text(text, size=10, overlap=5, strategy="word")
    assert len(chunks) == 3
    assert len(chunks[0].split()) == 10
    c1, c2 = chunks[0].split(), chunks[1].split()
    assert c1[-5:] == c2[:5]  # word-level overlap exact


def test_chunk_paragraph_strategy_respects_boundaries():
    paras = [
        f"\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 {i} " + "\u06a9\u0644\u0645\u0647 " * 8
        for i in range(4)
    ]  # 9 words each
    text = "\n\n".join(paras)
    chunks = chunk_text(text, size=20, overlap=5, strategy="paragraph")
    # paragraphs never split mid-way when they fit the budget
    assert all("\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641" in c for c in chunks)
    assert all(c.count("\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641") <= 2 for c in chunks)
    # overlap is paragraph-aligned
    if len(chunks) > 1:
        assert chunks[0] != chunks[1]


def test_auto_strategy_splits_oversized_paragraph_at_sentences():
    # one giant paragraph made of 3 sentences, budget fits 1.5 sentences
    text = (
        ("\u062c\u0645\u0644\u0647 \u0627\u0644\u0641 " * 12).strip()
        + ". "
        + ("\u062c\u0645\u0644\u0647 \u0628 " * 12).strip()
        + ". "
        + ("\u062c\u0645\u0644\u0647 \u062c " * 12).strip()
        + "."
    )
    chunks = chunk_text(text, size=20, overlap=4, strategy="auto")
    assert len(chunks) >= 3
    # sentence boundaries are preferred: chunk 1 ends at a sentence end
    assert chunks[0].rstrip().endswith(".")


def test_sentence_strategy_keeps_sentences_intact():
    sentences = [
        f"\u062c\u0645\u0644\u0647 {i} " + "\u06a9\u0644\u0645\u0647 " * 5 + "." for i in range(5)
    ]
    text = " ".join(sentences)
    chunks = chunk_text(text, size=15, overlap=3, strategy="sentence")
    assert all(c.strip().endswith(".") for c in chunks if c.strip())


def test_max_chunks_caps_output():
    text = " ".join(f"word{i}" for i in range(50))
    chunks = chunk_text(text, size=10, overlap=5, max_chunks=2)
    assert len(chunks) == 2


def test_hard_fallback_for_single_oversized_word_unit():
    # a single sentence larger than the budget in sentence mode → hard split
    text = " ".join(f"k{i}" for i in range(30))
    chunks = chunk_text(text, size=10, overlap=2, strategy="sentence")
    assert len(chunks) >= 3
    assert all(len(c.split()) <= 10 for c in chunks)


def test_rtl_and_zwj():
    text = "\u0633\u0644\u0627\u0645\u200c\u062f\u0646\u06cc\u0627 \u0627\u06cc\u0646 \u06cc\u06a9 \u062a\u0633\u062a \u0627\u0633\u062a"
    chunks = chunk_text(text, size=3, overlap=1)
    assert chunks[0] == "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627 \u0627\u06cc\u0646"
    # hard split continues into the next chunk (unit-aligned overlap)
    assert chunks[1] == "\u06cc\u06a9 \u062a\u0633\u062a \u0627\u0633\u062a"


def test_empty_input():
    assert chunk_text("", size=500) == []
    assert chunk_text("   ", size=500) == []
    assert chunk_text(None, size=500) == []


def test_count_words():
    assert count_words("\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627") == 2
    assert count_words("") == 0


def test_word_count_from_html():
    html = "<h2>\u062a\u06cc\u062a\u0631</h2><p>\u062f\u0648 \u06a9\u0644\u0645\u0647</p>"
    assert word_count_from_html(html) == 3
