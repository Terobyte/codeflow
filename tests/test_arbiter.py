from synapse.pipeline.arbiter import ArbiterPolicy


def fixed_splitter(text):
    return [text]  # one "sentence" per push -- deterministic for these tests


def test_speak_jumps_to_head_when_queue_empty():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["критично"]


def test_current_dispatcher_sentence_finishes_before_speak():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("первое.")
    a.push_dispatcher_text("второе.")
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["первое.", "критично"]


def test_speak_drops_undelivered_dispatcher_tail():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("первое.")
    a.push_dispatcher_text("второе.")
    a.push_dispatcher_text("третье.")
    a.push_speak("критично")
    items = a.drain_all()
    assert [i.text for i in items] == ["первое.", "критично"]


def test_flush_dispatcher_only_removes_dispatcher_items():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("критично")
    a.push_dispatcher_text("болтовня.")
    a.flush_dispatcher()
    items = a.drain_all()
    assert [i.text for i in items] == ["критично"]


def test_multiple_speaks_all_survive_a_dispatcher_flush():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_speak("первый спик")
    a.push_speak("второй спик")
    a.push_dispatcher_text("болтовня.")
    a.flush_dispatcher()
    items = [i.text for i in a.drain_all()]
    assert "первый спик" in items
    assert "второй спик" in items
    assert "болтовня." not in items


def test_default_splitter_splits_sentences():
    a = ArbiterPolicy()
    a.push_dispatcher_text("Привет. Как дела?")
    items = a.drain_all()
    assert len(items) == 2


def test_drain_all_empties_the_queue():
    a = ArbiterPolicy(splitter=fixed_splitter)
    a.push_dispatcher_text("текст")
    a.drain_all()
    assert len(a) == 0
    assert a.pop_next() is None
