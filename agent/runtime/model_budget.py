"""单次输入按UTF-8字节保守约束；旧任务缺字段时仍解释为原上限。"""
LEGACY_INPUT_BYTES = 6000
DEFAULT_INPUT_BYTES = 32768
MESSAGE_OVERHEAD = 256


def input_bytes(system, user):
    return len(system.encode('utf-8')) + len(user.encode('utf-8')) + MESSAGE_OVERHEAD
