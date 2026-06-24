"""core/totp.py 通用 TOTP 工具测试：归一、合法性判定、码生成。"""
from core.totp import generate_totp_code, is_valid_totp_secret, normalize_totp_secret


def test_normalize_otpauth_url_extracts_secret():
    url = "otpauth://totp/Google:user@gmail.com?secret=5djirenhw5dablrxbxn62trcpzp2t5vg&issuer=Google&algorithm=SHA1&digits=6&period=30"
    assert normalize_totp_secret(url) == "5DJIRENHW5DABLRXBXN62TRCPZP2T5VG"


def test_normalize_pure_base32_uppercases():
    assert normalize_totp_secret("5djirenhw5dablrxbxn62trcpzp2t5vg") == "5DJIRENHW5DABLRXBXN62TRCPZP2T5VG"


def test_normalize_strips_spaces():
    assert normalize_totp_secret("5 DJ IRE N HW5") == "5DJIRENHW5"


def test_normalize_recovery_code_passthrough():
    """recovery/备份码非 base32，原样 upper 返回（运行时 generate 跳过）。"""
    assert normalize_totp_secret("abc-123-xyz") == "ABC-123-XYZ"


def test_normalize_empty():
    assert normalize_totp_secret("") == ""
    assert normalize_totp_secret(None) == ""


def test_is_valid_totp_secret_true_for_base32():
    assert is_valid_totp_secret("5DJIRENHW5DABLRXBXN62TRCPZP2T5VG") is True
    assert is_valid_totp_secret("JBSWY3DPEHPK3PXP") is True


def test_is_valid_totp_secret_false_for_non_base32():
    assert is_valid_totp_secret("abc") is False
    assert is_valid_totp_secret("abc-123-xyz") is False
    assert is_valid_totp_secret("") is False
    assert is_valid_totp_secret("0189") is False  # 数字 0/1/8/9 不在 base32 字符集


def test_generate_totp_code_returns_six_digits_for_valid_secret():
    code = generate_totp_code("5DJIRENHW5DABLRXBXN62TRCPZP2T5VG")
    assert len(code) == 6
    assert code.isdigit()


def test_generate_totp_code_empty_for_invalid_secret():
    """recovery 码非 base32，generate 返回空串，调用方应跳过自动填码。"""
    assert generate_totp_code("recovery-code") == ""
    assert generate_totp_code("") == ""
    assert generate_totp_code(None) == ""


def test_generate_totp_code_accepts_lowercase_and_spaces():
    """normalize 后的 secret 也能直接 generate（内部会清洗）。"""
    code = generate_totp_code("5djirenhw5dablrxbxn62trcpzp2t5vg")
    assert len(code) == 6
    assert code.isdigit()
