from xiangkou.harness import AgentHarness, mask_personal_info


def test_contact_identifiers_are_masked_from_model_prompt_and_events():
    raw = "联系 13812345678 或 xiao@example.com 找火锅"
    masked = mask_personal_info(raw)
    assert "13812345678" not in masked and "xiao@example.com" not in masked
    assert "火锅" in masked
    assert AgentHarness._redact({"message": raw})["message"] == masked
