"""脱敏：既要挡住真号码，又不能把正常数据改乱。

第二条同样重要——脱敏改乱了数据，模型反而会判断错列的含义。
"""

from __future__ import annotations

from wenhui.privacy import build_masked_samples, mask_text


def test_身份证打码后看不出原号():
    masked = mask_text("330102199001011234")
    assert masked == "3301**********1234"
    assert "19900101" not in masked


def test_手机号只留前三后四():
    assert mask_text("13812345678") == "138****5678"


def test_银行卡只留后四位且长度不变():
    masked = mask_text("6222021234567890")
    assert masked.endswith("7890")
    assert masked[:-4] == "*" * 12


def test_邮箱只留首字母和域名():
    assert mask_text("zhangsan@example.com") == "z***@example.com"


def test_正常数据一个都不碰():
    """人数、年份、金额这些短数字不能被当成号码打掉。"""
    for text in ("共320人", "2026年9月", "教授", "计算机学院", "120"):
        assert mask_text(text) == text


def test_一句话里混着多个号码也认得出来():
    masked = mask_text("张三 330102199001011234 电话 13812345678")
    assert "330102199001011234" not in masked
    assert "13812345678" not in masked
    assert "3301**********1234" in masked
    assert "138****5678" in masked


def test_样本去重发生在打码之后():
    """同一个号码的不同写法打码后应该只留一条，而不是重复出现。"""
    samples = build_masked_samples(["13812345678", "13812345678", "13899998888"], limit=5)
    assert samples == ["138****5678", "138****8888"]


def test_样本数量有上限():
    values = [f"1381234567{i}" for i in range(9)]
    assert len(build_masked_samples(values, limit=3)) == 3


def test_可以只开一部分规则():
    text = "330102199001011234 13812345678"
    only_phone = mask_text(text, enabled={"mask_phone"})
    assert "330102199001011234" in only_phone      # 身份证规则没开，保持原样
    assert "138****5678" in only_phone
