"""令牌生成与正则测试。"""

from mctgauth_bot.tokens import TOKEN_ALPHABET, TOKEN_RE, generate_token


def test_token_length_and_alphabet():
    for _ in range(200):
        tok = generate_token()
        assert len(tok) == 8
        assert all(c in TOKEN_ALPHABET for c in tok)


def test_token_no_ambiguous_chars():
    # 字母表不含易混淆字符 I、O、0、1。
    for bad in "IO01":
        assert bad not in TOKEN_ALPHABET
    for _ in range(200):
        tok = generate_token()
        assert not any(c in "IO01" for c in tok)


def test_token_regex_matches_generated():
    for _ in range(50):
        assert TOKEN_RE.match(generate_token())


def test_token_regex_rejects_bad():
    assert not TOKEN_RE.match("ABCDEFG")      # 7 位
    assert not TOKEN_RE.match("ABCDEFGHJ")    # 9 位
    assert not TOKEN_RE.match("ABCDEFGI")     # 含 I
    assert not TOKEN_RE.match("ABCDEF00")     # 含 0
    assert not TOKEN_RE.match("abcdefgh")     # 小写
    assert not TOKEN_RE.match("ABCD EFG")     # 空格


def test_token_reasonable_uniqueness():
    tokens = {generate_token() for _ in range(1000)}
    # 8 位、32 字母表，1000 次几乎不应大量碰撞。
    assert len(tokens) > 990
