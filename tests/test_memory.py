from dafeiyu.memory import MemoryStore


def test_append_and_history_trim():
    store = MemoryStore(max_turns=2)
    key = store.private_key("10001")
    for i in range(4):
        store.append(key, "user", f"问{i}")
        store.append(key, "assistant", f"答{i}")
    history = store.history(key)
    assert len(history) == 4  # 2 轮 = 4 条
    assert history[0] == {"role": "user", "content": "问2"}
    assert history[-1] == {"role": "assistant", "content": "答3"}


def test_sessions_are_isolated():
    store = MemoryStore()
    a = store.group_key("111", "1")
    b = store.group_key("111", "2")
    store.append(a, "user", "我叫小明")
    assert store.history(a)[0]["content"] == "我叫小明"
    assert store.history(b) == []


def test_reset_clears_session():
    store = MemoryStore()
    key = store.private_key("1")
    store.append(key, "user", "hi")
    store.reset(key)
    assert store.history(key) == []


def test_ttl_expiry():
    now = [0.0]
    store = MemoryStore(ttl_seconds=60, clock=lambda: now[0])
    key = store.private_key("1")
    store.append(key, "user", "hi")
    now[0] = 30
    assert len(store) == 1
    now[0] = 61
    assert len(store) == 0


def test_max_sessions_eviction():
    now = [0.0]
    store = MemoryStore(max_sessions=2, clock=lambda: now[0])
    for i in range(3):
        now[0] += 1
        store.append(store.private_key(str(i)), "user", "hi")
    assert len(store) == 2
    assert store.history(store.private_key("0")) == []  # 最久未用被淘汰
    assert store.history(store.private_key("2"))[0]["content"] == "hi"
