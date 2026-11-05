"""Shared user-facing website notice for WeChat and QQ deliveries."""


def build_access_message(report_day: str, access_url: str, bot_name: str) -> str:
    return (
        f"{report_day} 总结网站访问信息\n网址：{access_url}\n"
        f"使用：@{bot_name} 总结（默认最近24小时）\n"
        f"示例：@{bot_name} 总结最近3h聊天记录（时长可用自然语言指定）"
    )
