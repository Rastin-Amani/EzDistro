from app.domain.chunker import (
    chunk_text,
    count_words,
    split_paragraphs,
    split_sentences,
    strip_html,
    word_count_from_html,
)


def test_strip_html_removes_tags_and_scripts():
    raw = "<p>hello world</p><script>var x = 1;</script><style>body{}</style><div>second</div>"
    text = strip_html(raw)
    assert "hello world" in text
    assert "var x" not in text
    assert "second" in text
    assert "<" not in text


def test_strip_html_unescapes_entities():
    assert strip_html("<p>a &amp; b</p>") == "a & b"


def test_paragraph_splitting():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    paras = split_paragraphs(text)
    assert len(paras) == 3
    assert paras[0] == "First paragraph."


def test_sentence_splitting():
    text = (
        "First sentence. Second sentence? Third sentence! Fourth sentence. Fifth sentence"
        "\nSixth sentence"
    )
    sentences = split_sentences(text)
    assert len(sentences) == 6
    assert sentences[0] == "First sentence."
    assert sentences[1] == "Second sentence?"
    assert "Sixth sentence" in sentences[-1]


def test_chunk_size_and_overlap_word_strategy():
    text = " ".join(f"word{i}" for i in range(20))
    chunks = chunk_text(text, size=10, overlap=5, strategy="word")
    assert len(chunks) == 3
    assert len(chunks[0].split()) == 10
    c1, c2 = chunks[0].split(), chunks[1].split()
    assert c1[-5:] == c2[:5]  # word-level overlap exact


def test_chunk_paragraph_strategy_respects_boundaries():
    paras = [f"Paragraph {i} " + "word " * 8 for i in range(4)]  # 9 words each
    text = "\n\n".join(paras)
    chunks = chunk_text(text, size=20, overlap=5, strategy="paragraph")
    # paragraphs never split mid-way when they fit the budget
    assert all("Paragraph" in c for c in chunks)
    assert all(c.count("Paragraph") <= 2 for c in chunks)
    # overlap is paragraph-aligned
    if len(chunks) > 1:
        assert chunks[0] != chunks[1]


def test_auto_strategy_splits_oversized_paragraph_at_sentences():
    # one giant paragraph made of 3 sentences, budget fits 1.5 sentences
    text = (
        ("Sentence A " * 12).strip()
        + ". "
        + ("Sentence B " * 12).strip()
        + ". "
        + ("Sentence C " * 12).strip()
        + "."
    )
    chunks = chunk_text(text, size=20, overlap=4, strategy="auto")
    assert len(chunks) >= 3
    # sentence boundaries are preferred: chunk 1 ends at a sentence end
    assert chunks[0].rstrip().endswith(".")


def test_sentence_strategy_keeps_sentences_intact():
    sentences = [f"Sentence {i} " + "word " * 5 + "." for i in range(5)]
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


def test_zwj_normalization():
    text = "hello\u200cworld this is a test"
    chunks = chunk_text(text, size=3, overlap=1)
    assert chunks[0] == "hello world this"
    # hard split continues into the next chunk (unit-aligned overlap)
    assert chunks[1] == "is a test"


def test_empty_input():
    assert chunk_text("", size=500) == []
    assert chunk_text("   ", size=500) == []
    assert chunk_text(None, size=500) == []


def test_count_words():
    assert count_words("hello world") == 2
    assert count_words("") == 0


def test_word_count_from_html():
    html = "<h2>Heading</h2><p>two word</p>"
    assert word_count_from_html(html) == 3
